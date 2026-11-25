"""Execute the notebook's public workflow rather than mirroring its implementation."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.labeling import pool_fingerprint
from matching.auto_matcher import write_auto_labels
from matching.dataset_plan import personal_manual_plan

ROOT = Path(__file__).resolve().parents[1]


def code_cells():
    notebook = json.loads((ROOT / 'notebooks/labeling_tool.ipynb').read_text(encoding='utf-8'))
    assert all(not c.get('outputs') and c.get('execution_count') is None
               for c in notebook['cells'] if c['cell_type'] == 'code')
    return [''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code']


def test_all_six_notebook_cells_run_in_demo_and_save_only_after_a_human_click(monkeypatch):
    pytest.importorskip('ipywidgets')
    import IPython.display
    monkeypatch.setattr(IPython.display, 'display', lambda *args, **kwargs: None)
    monkeypatch.chdir(ROOT)
    codes = code_cells()
    assert len(codes) == 6
    ns = {'__name__': '__notebook__'}
    try:
        for i, code in enumerate(codes):
            exec(compile(code, f'labeling_tool:cell{i + 1}', 'exec'), ns)
            if i == 0:
                ns.update(DEMO=True, ON_COLAB=False, INSTALL_PACKAGES=False)
            if i == 3:
                ui = ns['labeling_ui']
                assert not ns['progress'].exists()
                next(b for b in ui.actions.children
                     if getattr(b, 'description', '') == 'Không có match').click()
                assert len(ui.session.labels) == 1
        assert pq.read_table(ns['progress']).num_rows == 1
        assert ns['labeling_ui'].closed
    finally:
        if 'labeling_ui' in ns:
            ns['labeling_ui'].close()
        if 'demo_directory' in ns:
            ns['demo_directory'].cleanup()


def test_notebook_review_selects_only_pending_auto_keys_and_isolates_run_progress(tmp_path):
    entries = pa.Table.from_pylist([dict(arxiv_id='p', citation_key='k', title='Example',
                                       authors=['Jane Doe'])])
    references = pa.Table.from_pylist([
        dict(arxiv_id='p', ref_arxiv_id=r, ref_title='Example', ref_authors=['Jane Doe'])
        for r in ('r1', 'r2')])
    bib = tmp_path / 'bib.parquet'
    refs = tmp_path / 'refs.parquet'
    pq.write_table(entries, bib)
    pq.write_table(references, refs)
    p = build_candidate_pool(entries, references)
    plan = tmp_path / 'dataset_plan.json'
    plan.write_text(json.dumps({'candidate_pool_sha256': pool_fingerprint(p)}), encoding='utf-8')
    auto = tmp_path / 'auto-run-v2'
    write_auto_labels(p, auto)
    import matching.auto_matcher as matcher_module
    ns = dict(DEMO=False, MODE='review', MEMBER='member1', REVIEWER='hieu',
        BIBTEX_PATH=bib, REFERENCES_PATH=refs, DOI_INDEX=None, PLAN_PATH='',
        AUTO_RUN_DIR=auto, PROGRESS_ROOT=tmp_path, json=json, pq=pq, pc=pa.compute,
        build_candidate_pool=build_candidate_pool, pool_fingerprint=pool_fingerprint,
        matcher_module=matcher_module, Path=Path)
    exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)
    assert ns['entry_keys'] == [('p', 'k')]
    assert ns['progress'] == tmp_path / 'review/auto-run-v2/hieu.parquet'
    assert not ns['progress'].exists()


def test_notebook_refuses_to_prepare_manual_on_a_different_snapshot(tmp_path):
    entries = pa.Table.from_pylist([dict(arxiv_id='p', citation_key='k')])
    references = pa.table({'arxiv_id': pa.array([], pa.string()),
                           'ref_arxiv_id': pa.array([], pa.string())})
    bib, refs = tmp_path / 'bib.parquet', tmp_path / 'refs.parquet'
    pq.write_table(entries, bib)
    pq.write_table(references, refs)
    plan = tmp_path / 'dataset_plan.json'
    plan.write_text(json.dumps({'candidate_pool_sha256': 'different'}), encoding='utf-8')
    ns = dict(DEMO=False, MODE='manual', PLAN_MODE='existing', MEMBER='member1', REVIEWER='hieu',
        BIBTEX_PATH=bib, REFERENCES_PATH=refs, DOI_INDEX=None, PLAN_PATH=plan,
        json=json, build_candidate_pool=build_candidate_pool, pool_fingerprint=pool_fingerprint,
        Path=Path, PROGRESS_ROOT=tmp_path)
    with pytest.raises(ValueError, match='Snapshot khác'):
        exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)


def test_shared_notebook_template_leaves_all_data_and_output_paths_blank(monkeypatch):
    monkeypatch.chdir(ROOT)
    ns = {}
    exec(compile(code_cells()[0], 'labeling_tool:cell1', 'exec'), ns)
    for name in ('BIBTEX_PATH', 'REFERENCES_PATH', 'PLAN_PATH', 'PROGRESS_ROOT',
                 'DOI_INDEX', 'AUTO_RUN_DIR', 'MEMBER', 'REVIEWER'):
        assert ns[name] == ''
    assert 'DATA_ROOT' not in ns and 'DATASET_ROOT' not in ns
    assert ns['GROUP_MEMBERS'] == 4 and ns['PLAN_MODE'] == 'personal'
    assert ns['CHECKPOINT_PATH'] == ns['REFERENCE_SNAPSHOTS_PATH'] == ''


@pytest.mark.parametrize('missing', ['BIBTEX_PATH', 'REFERENCES_PATH',
                                    'PROGRESS_ROOT', 'AUTO_RUN_DIR'])
def test_blank_required_paths_are_named_before_filesystem_access(tmp_path, missing):
    ns = dict(DEMO=False, MODE='review', MEMBER='member1', REVIEWER='hieu',
              Path=Path, DOI_INDEX='', **{name: str(tmp_path / 'not-read-yet') for name in
              ('BIBTEX_PATH', 'REFERENCES_PATH', 'PLAN_PATH', 'PROGRESS_ROOT', 'AUTO_RUN_DIR')})
    ns[missing] = ''
    with pytest.raises(ValueError, match='Chưa điền đường dẫn.*' + missing):
        exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)


def test_personal_notebook_creates_and_resumes_own_plan_without_group_data(tmp_path):
    entries = pa.Table.from_pylist([dict(arxiv_id=f'p{i}', citation_key=f'k{k}', title='Example')
                                    for i in range(6) for k in range(23)])
    refs = pa.Table.from_pylist([dict(arxiv_id=f'p{i}', ref_arxiv_id=f'r{i}') for i in range(6)])
    bib_path, ref_path = tmp_path / 'bib.parquet', tmp_path / 'refs.parquet'
    pq.write_table(entries, bib_path)
    pq.write_table(refs, ref_path)
    ns = dict(DEMO=False, MODE='manual', PLAN_MODE='personal', MEMBER='member4', REVIEWER='fourth',
        BIBTEX_PATH=str(bib_path), REFERENCES_PATH=str(ref_path), PLAN_PATH='',
        PROGRESS_ROOT=str(tmp_path / 'my-labels'), DOI_INDEX='', Path=Path, json=json,
        build_candidate_pool=build_candidate_pool, pool_fingerprint=pool_fingerprint,
        personal_manual_plan=personal_manual_plan, GROUP_MEMBERS=4, SEED=42,
        MANUAL_PUBLICATIONS=[], EXCLUDED_PUBLICATIONS=[])
    code = compile(code_cells()[2], 'labeling_tool:cell3', 'exec')
    exec(code, ns)
    assert len(ns['entry_keys']) == 100 and len(ns['publications']) == 5
    assert not ns['progress'].exists()
    plan_path = tmp_path / 'my-labels/plans/member4.json'
    original = plan_path.read_bytes()
    from matching.labeling_table import TableSession
    session = TableSession(ns['pool'], ns['progress'], reviewer='fourth',
                           publications=ns['publications'], entry_keys=ns['entry_keys'])
    session.save(None)
    exec(code, ns)
    assert plan_path.read_bytes() == original
    assert pq.read_table(ns['progress']).num_rows == 1


def test_existing_mode_requires_original_plan_path_before_reading_files(tmp_path):
    ns = dict(DEMO=False, MODE='manual', PLAN_MODE='existing', MEMBER='member1', REVIEWER='hieu',
        BIBTEX_PATH='bib.parquet', REFERENCES_PATH='refs.parquet', PLAN_PATH='',
        PROGRESS_ROOT=tmp_path, DOI_INDEX=None)
    with pytest.raises(ValueError, match='Chưa điền đường dẫn.*PLAN_PATH'):
        exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)


def test_all_six_cells_work_on_a_personal_dataset_and_keep_saved_labels_on_restart(tmp_path, monkeypatch):
    pytest.importorskip('ipywidgets')
    import IPython.display
    monkeypatch.setattr(IPython.display, 'display', lambda *args, **kwargs: None)
    monkeypatch.chdir(ROOT)
    bib, refs = tmp_path / 'own-bib.parquet', tmp_path / 'own-refs.parquet'
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id=f'own{i}', citation_key=f'k{k}', title='Own entry', authors=['Jane Doe'])
        for i in range(6) for k in range(22)]), bib)
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id=f'own{i}', ref_arxiv_id=f'r{i}', ref_title='Own reference')
        for i in range(6)]), refs)
    ns = {'__name__': '__notebook__'}
    try:
        for i, code in enumerate(code_cells()):
            exec(compile(code, f'labeling_tool:cell{i + 1}', 'exec'), ns)
            if i == 0:
                ns.update(ON_COLAB=False, INSTALL_PACKAGES=False, MEMBER='member4', REVIEWER='fourth',
                          BIBTEX_PATH=str(bib), REFERENCES_PATH=str(refs),
                          PROGRESS_ROOT=str(tmp_path / 'progress'))
            if i == 3:
                ui = ns['labeling_ui']
                ui.table.children[2].children[0].click()
                assert len(ui.session.labels) == 1
        assert ns['labeling_ui'].closed
        before = ns['personal_path'].read_bytes()
        exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)
        exec(compile(code_cells()[3], 'labeling_tool:cell4', 'exec'), ns)
        assert len(ns['labeling_ui'].session.labels) == 1
        assert len(ns['labeling_ui'].session.keys) == 100
        assert ns['personal_path'].read_bytes() == before
    finally:
        if 'labeling_ui' in ns:
            ns['labeling_ui'].close()
