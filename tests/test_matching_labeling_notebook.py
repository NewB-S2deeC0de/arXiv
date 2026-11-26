"""Test the shared notebook through the form callbacks users execute."""
import json
from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.labeling import pool_fingerprint, run_labeling_session
from matching.auto_matcher import write_auto_labels
from matching.notebook_tool import AnnotationConfig, AnnotationNotebook, PathPicker, prepare_annotation, resolve_identity

ROOT = Path(__file__).resolve().parents[1]


def codes():
    n = json.loads((ROOT / 'notebooks/labeling_tool.ipynb').read_text(encoding='utf-8'))
    assert all(not c.get('outputs') and c.get('execution_count') is None
               for c in n['cells'] if c['cell_type'] == 'code')
    return [''.join(c['source']) for c in n['cells'] if c['cell_type'] == 'code']


def inputs(tmp_path):
    bib, refs = tmp_path / 'bib.parquet', tmp_path / 'refs.parquet'
    entries = pa.Table.from_pylist([dict(arxiv_id=f'p{i}', citation_key=f'k{k}',
        title='Example', authors=['Jane Doe']) for i in range(6) for k in range(23)])
    references = pa.Table.from_pylist([dict(arxiv_id=f'p{i}', ref_arxiv_id=f'r{i}',
        ref_title='Example', ref_authors=['Jane Doe']) for i in range(6)])
    pq.write_table(entries, bib)
    pq.write_table(references, refs)
    return AnnotationConfig(member='member4', reviewer='fourth', bibtex=str(bib),
        references=str(refs), output=str(tmp_path / 'my-labels'))


def test_shared_notebook_has_no_personal_configuration_to_edit():
    cells = codes()
    assert len(cells) == 4
    for i, code in enumerate(cells):
        compile(code, f'labeling_tool:cell{i+1}', 'exec')
    assert not any(x in '\n'.join(cells) for x in ('Data_Crawl_Last', 'BIBTEX_PATH =',
        'REFERENCES_PATH =', 'MEMBER =', 'GROUP_MEMBERS =', 'ROOT ='))
    c = AnnotationConfig()
    assert c.member == c.reviewer == c.bibtex == c.references == c.output == ''
    assert c.plan == c.doi == c.auto_run == ''


def test_four_cells_run_demo_and_save_only_after_a_human_click(monkeypatch):
    pytest.importorskip('ipywidgets')
    import IPython.display
    monkeypatch.setattr(IPython.display, 'display', lambda *a, **k: None)
    monkeypatch.chdir(ROOT)
    ns = {'__name__': '__notebook__'}
    tool = None
    try:
        for i, code in enumerate(codes()):
            exec(compile(code, f'labeling_tool:cell{i+1}', 'exec'), ns)
            if i == 1:
                tool = ns['tool']
                tool.task.value = 'demo'
                tool.open_button.click()
                assert tool.prepared is not None
                assert not tool.prepared.progress.exists()
                next(b for b in tool.table.actions.children
                     if getattr(b, 'description', '') == 'Không có match').click()
                assert pq.read_table(tool.prepared.progress).num_rows == 1
        assert tool.table.closed
    finally:
        if tool:
            tool.close()
            if tool.temporary:
                tool.temporary.cleanup()


def test_personal_form_creates_plan_and_profile_resumes_without_resampling(tmp_path):
    pytest.importorskip('ipywidgets')
    c = inputs(tmp_path)
    tool, restored = AnnotationNotebook(start_directory=tmp_path), None
    try:
        tool.member.value, tool.reviewer.value = 'member4', 'fourth'
        for name in ('bibtex', 'references', 'output'):
            tool.fields[name].value = getattr(c, name)
        tool.open_button.click()
        assert tool.prepared is not None, tool.status.value
        assert len(tool.prepared.entry_keys) == 100
        assert not tool.prepared.progress.exists()
        tool.table.table.children[2].children[0].click()
        assert len(tool.table.session.labels) == 1
        original_plan, profile = tool.prepared.plan_path.read_bytes(), tool.profile.value
        assert Path(profile).is_file()
        tool.finish()
        restored = AnnotationNotebook(start_directory=tmp_path)
        restored.profile.value = profile
        restored.root.children[1].children[-1].click()
        assert restored.member.value == 'member4'
        restored.open_button.click()
        assert restored.prepared is not None, restored.status.value
        assert len(restored.table.session.labels) == 1
        assert restored.prepared.plan_path.read_bytes() == original_plan
        assert len(restored.table.session.keys) == 100
    finally:
        tool.close()
        if restored:
            restored.close()


def test_existing_mode_keeps_legacy_progress_and_rejects_different_snapshot(tmp_path):
    c = inputs(tmp_path)
    pool = build_candidate_pool(c.bibtex, c.references)
    keys = [(r['arxiv_id'], r['citation_key']) for r in pool.entries.to_pylist()[:20]]
    plan = tmp_path / 'original-plan.json'
    plan.write_text(json.dumps({'candidate_pool_sha256': pool_fingerprint(pool),
        'manual_assignments': [dict(owner='member1', arxiv_id='p0', citation_keys=[k[1] for k in keys])]}),
        encoding='utf-8')
    progress = Path(c.output) / 'manual/member1.parquet'
    run_labeling_session(pool, progress, reviewer='hieu', entry_keys=[keys[0]],
                         input_fn=lambda _: '1', output=lambda _: None)
    original = progress.read_bytes()
    c.task, c.member, c.reviewer, c.plan = 'existing', 'member1', 'hieu', str(plan)
    prepared = prepare_annotation(c)
    assert prepared.progress == progress and progress.read_bytes() == original
    payload = json.loads(plan.read_text(encoding='utf-8'))
    payload['candidate_pool_sha256'] = 'wrong'
    plan.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='snapshot'):
        prepare_annotation(c)
    assert progress.read_bytes() == original


def test_review_uses_correct_auto_run_without_manual_plan(tmp_path):
    c = inputs(tmp_path)
    pool = build_candidate_pool(c.bibtex, c.references)
    rows = pool.references.to_pylist()
    rows.append(dict(rows[0], ref_arxiv_id='another-r0'))
    pq.write_table(pa.Table.from_pylist(rows, schema=pool.references.schema), c.references)
    pool = build_candidate_pool(c.bibtex, c.references)
    auto = tmp_path / 'auto-v2'
    write_auto_labels(pool, auto)
    c.task, c.auto_run = 'review', str(auto)
    prepared = prepare_annotation(c)
    assert prepared.plan_path is None and len(prepared.entry_keys) == 23
    assert prepared.progress == Path(c.output) / 'review/auto-v2/member4.parquet'
    assert not prepared.progress.exists()


@pytest.mark.parametrize('field,label', [('bibtex', 'BibTeX M2'), ('references', 'References M1'),
                                        ('output', 'thư mục lưu nhãn')])
def test_required_fields_are_named_in_errors(tmp_path, field, label):
    c = inputs(tmp_path)
    setattr(c, field, '')
    with pytest.raises(ValueError, match=label):
        prepare_annotation(c)


def test_web_link_is_not_mistaken_for_a_drive_path(tmp_path):
    c = inputs(tmp_path)
    c.bibtex = 'https://drive.google.com/file/d/example/view'
    with pytest.raises(ValueError, match='link website Drive'):
        prepare_annotation(c)


def test_hidden_fields_are_ignored_and_identity_change_closes_old_buttons(tmp_path):
    pytest.importorskip('ipywidgets')
    c = inputs(tmp_path)
    tool = AnnotationNotebook(start_directory=tmp_path)
    try:
        tool.member.value = 'member1'
        tool.reviewer.value = 'hieu'
        for name in ('bibtex', 'references', 'output'):
            tool.fields[name].value = getattr(c, name)
        tool.fields['plan'].value = 'hidden-plan-from-old-task.json'
        tool.fields['auto_run'].value = 'hidden-auto'
        tool.open_button.click()
        assert tool.prepared is not None, tool.status.value
        button = tool.table.table.children[2].children[0]
        path = tool.prepared.progress
        tool.member.value = 'member2'
        assert tool.table.closed and tool.prepared is None
        button.click()
        assert not path.exists() and tool.reviewer.value == 'hieu'
    finally:
        tool.close()


def test_picker_navigates_and_distinguishes_file_from_directory(tmp_path):
    pytest.importorskip('ipywidgets')
    import ipywidgets as w
    folder = tmp_path / 'sub'
    folder.mkdir()
    file = folder / 'plan.json'
    file.write_text('{}', encoding='utf-8')
    field = w.Text()
    picker = PathPicker(field, start=tmp_path, kind='file')
    try:
        picker.items.value = str(folder)
        picker._select()
        assert field.value == ''
        picker._open()
        assert Path(picker.location.value) == folder
        picker.items.value = str(file)
        picker._select()
        assert field.value == str(file)
    finally:
        picker.close()
        field.close()


def test_progress_and_finish_before_open_are_friendly_not_name_errors(tmp_path):
    pytest.importorskip('ipywidgets')
    tool = AnnotationNotebook(start_directory=tmp_path)
    try:
        tool.show_progress()
        assert 'biểu mẫu' in tool.report_area.children[0].value
        tool.finish()
        assert 'Chưa mở bảng' in tool.report_area.children[0].value
    finally:
        tool.close()


def test_doi_is_a_visible_main_form_row_and_does_not_depend_on_accordion(tmp_path):
    pytest.importorskip('ipywidgets')
    tool = AnnotationNotebook(start_directory=tmp_path)
    try:
        children = list(tool.root.children)
        assert children.index(tool.path_rows['doi']) == children.index(tool.path_rows['references']) + 1
        assert tool.path_rows['doi'].layout.display != 'none'
        assert tool.advanced_box.layout.display == 'none'
        tool.task.value = 'existing'
        assert tool.path_rows['doi'].layout.display != 'none'
        tool.advanced_toggle.value = True
        assert tool.advanced_box.layout.display != 'none'
        tool.advanced_toggle.value = False
        assert tool.advanced_box.layout.display == 'none'
        assert tool.path_rows['doi'].layout.display != 'none'
    finally:
        tool.close()


@pytest.mark.parametrize('name,code', [('Tân', 'tan'), ('Đặng Văn Tân', 'dang_van_tan'),
                                      ('Người dùng 17', 'nguoi_dung_17')])
def test_identity_accepts_unicode_names_and_generates_portable_ids(name, code):
    config = resolve_identity(AnnotationConfig(reviewer=name))
    assert config.member == code and config.reviewer == name
    assert resolve_identity(AnnotationConfig(member='custom_user_99', reviewer=name)).member == 'custom_user_99'


def test_existing_identity_cannot_guess_a_new_filename_from_name():
    with pytest.raises(ValueError, match='đúng mã lưu tiến độ'):
        resolve_identity(AnnotationConfig(task='existing', reviewer='Hiếu'))
    with pytest.raises(ValueError, match='không điền đường dẫn'):
        resolve_identity(AnnotationConfig(member='../elsewhere', reviewer='Tân'))


def test_free_name_form_has_no_roster_and_saves_unicode_name_with_generated_id(tmp_path):
    pytest.importorskip('ipywidgets')
    import ipywidgets as w
    c = inputs(tmp_path)
    tool = AnnotationNotebook(start_directory=tmp_path)
    restored = None
    try:
        assert isinstance(tool.member, w.Text) and isinstance(tool.reviewer, w.Text)
        assert tool.member.value == tool.reviewer.value == ''
        tool.reviewer.value = 'Tân'
        for name in ('bibtex', 'references', 'output'):
            tool.fields[name].value = getattr(c, name)
        tool.open_button.click()
        assert tool.prepared is not None, tool.status.value
        assert tool.member.value == 'tan'
        assert tool.prepared.progress == Path(c.output) / 'manual/tan.parquet'
        tool.table.table.children[2].children[0].click()
        saved = pq.read_table(tool.prepared.progress).to_pylist()
        assert len(saved) == 1 and saved[0]['reviewer'] == 'Tân'
        profile = Path(tool.profile.value)
        assert json.loads(profile.read_text(encoding='utf-8'))['schema_version'] == 2
        restored = AnnotationNotebook(start_directory=tmp_path)
        restored.profile.value = str(profile)
        restored.root.children[1].children[-1].click()
        assert restored.reviewer.value == 'Tân' and restored.member.value == 'tan'
        restored.open_button.click()
        assert restored.prepared is not None, restored.status.value
        assert len(restored.table.session.labels) == 1
    finally:
        tool.close()
        if restored:
            restored.close()


def test_two_names_with_same_generated_id_do_not_silently_share_personal_plan(tmp_path):
    c = inputs(tmp_path)
    c.member, c.reviewer = '', 'Tân'
    prepared = prepare_annotation(c)
    original = prepared.plan_path.read_bytes()
    c.reviewer = 'Tấn'
    with pytest.raises(ValueError, match='tên người gán nhãn khác'):
        prepare_annotation(c)
    assert prepared.plan_path.read_bytes() == original


def test_v1_review_profile_keeps_legacy_filename_for_any_user_id(tmp_path):
    pytest.importorskip('ipywidgets')
    c = inputs(tmp_path)
    pool = build_candidate_pool(c.bibtex, c.references)
    rows = pool.references.to_pylist()
    rows.append(dict(rows[0], ref_arxiv_id='another-r0'))
    pq.write_table(pa.Table.from_pylist(rows, schema=pool.references.schema), c.references)
    pool = build_candidate_pool(c.bibtex, c.references)
    auto = tmp_path / 'auto-old'
    write_auto_labels(pool, auto)
    c.task, c.member, c.reviewer, c.auto_run = 'review', 'member27', 'legacy_user', str(auto)
    progress = Path(c.output) / 'review/auto-old/legacy_user.parquet'
    run_labeling_session(pool, progress, reviewer='legacy_user', entry_keys=[('p0', 'k0')],
                         input_fn=lambda _: '1', output=lambda _: None)
    original = progress.read_bytes()
    profile = tmp_path / 'old-profile.json'
    profile.write_text(json.dumps({'schema_version': 1, 'configuration': asdict(c)}), encoding='utf-8')
    tool = AnnotationNotebook(start_directory=tmp_path)
    try:
        tool.profile.value = str(profile)
        tool.root.children[1].children[-1].click()
        assert tool.member.value == 'legacy_user'
        tool.open_button.click()
        assert tool.prepared is not None, tool.status.value
        assert tool.prepared.progress == progress
        assert len(tool.table.session.labels) == 1
        assert progress.read_bytes() == original
    finally:
        tool.close()
