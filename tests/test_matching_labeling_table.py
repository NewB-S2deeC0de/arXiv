import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.labeling import run_labeling_session, pool_fingerprint
from matching.labeling_table import TableSession, LabelingTable, entry_html, candidate_cells_html


def pool():
    return build_candidate_pool(pa.Table.from_pylist([
        dict(arxiv_id='p', citation_key='a', title='Methods <script>alert(1)</script>',
             authors=['Jane Doe'], year=2024),
        dict(arxiv_id='p', citation_key='b', title='Second entry'),
        dict(arxiv_id='q', citation_key='c', title='No candidates')]),
        pa.Table.from_pylist([
            dict(arxiv_id='p', ref_arxiv_id='r1', ref_title='Méthods for matching',
                 ref_authors=['Jane Doe'], ref_submission_date='2024-01-01'),
            dict(arxiv_id='p', ref_arxiv_id='r2', ref_title='Other', ref_authors=['Person'])]))


def test_legacy_labels_resume_with_enrichment_without_changing_fingerprint(tmp_path):
    p = pool()
    path = tmp_path / 'member1.parquet'
    run_labeling_session(p, path, reviewer='hieu', entry_keys=[('p', 'a')],
                         input_fn=lambda _: '1', output=lambda _: None)
    original = path.read_bytes()
    session = TableSession(p, path, reviewer='hieu', papers=pa.table({
        'arxiv_id': ['r1'], 'doi': ['10.1234/recovered']}))
    assert session.key == ('p', 'b')
    assert session.dois[('p', 'r1')]['values'] == ['10.1234/recovered']
    assert session.fingerprint == pool_fingerprint(p)
    assert path.read_bytes() == original
    assert session.labels[('p', 'a')]['groundtruth_arxiv_id'] == 'r1'


def test_search_uses_enriched_metadata_preserves_full_candidates_and_saves_stable_id(tmp_path):
    s = TableSession(pool(), tmp_path / 'p.parquet', reviewer='hieu', papers=pa.table({
        'arxiv_id': ['r1'], 'doi': ['10.1234/recovered']}))
    assert [x['ref_arxiv_id'] for x in s.visible_candidates('methods DOE')] == ['r1']
    assert [x['ref_arxiv_id'] for x in s.visible_candidates('recovered')] == ['r1']
    assert s.visible_candidates('nothing') == []
    assert len(s.visible_candidates()) == 2
    s.save('r2', expected_key=('p', 'a'))
    assert pq.read_table(s.progress)['groundtruth_arxiv_id'].to_pylist() == ['r2']
    with pytest.raises(ValueError, match='không thuộc'):
        s.save('outside')


def test_interleaved_sessions_keep_other_entries_and_reject_same_entry_conflict(tmp_path):
    path = tmp_path / 'p.parquet'
    first = TableSession(pool(), path, reviewer='hieu')
    other = TableSession(pool(), path, reviewer='hieu')
    first.save('r1')
    with pytest.raises(RuntimeError, match='phiên khác'):
        other.save('r2')
    other.index = 1
    other.save(None)
    first.refresh()
    assert len(first.labels) == 2
    assert first.labels[('p', 'a')]['groundtruth_arxiv_id'] == 'r1'
    assert first.labels[('p', 'b')]['label_status'] == 'no_match'


def test_filter_scope_skip_clear_and_pending_progress(tmp_path):
    s = TableSession(pool(), tmp_path / 'p.parquet', reviewer='hieu',
                     publications=['p'], entry_keys=[('p', 'a'), ('p', 'b')])
    s.index = 1  # skipping an entry does not create a label
    s.save(None)
    assert s.next_pending() and s.key == ('p', 'a')
    s.save('r1')
    assert not s.next_pending()
    assert s.progress_rows() == [dict(arxiv_id='p', selected_entries=2, confirmed=2, pending=0)]
    s.save(clear=True)
    assert s.progress_rows()[0]['pending'] == 1
    with pytest.raises(RuntimeError, match='Entry đã đổi'):
        s.save('r1', expected_key=('q', 'c'))


def test_changed_snapshot_is_rejected_and_html_is_escaped(tmp_path):
    p = pool()
    path = tmp_path / 'p.parquet'
    s = TableSession(p, path, reviewer='hieu')
    s.save(None)
    html = entry_html(s)
    assert '<script>' not in html and '&lt;script&gt;' in html
    changed = build_candidate_pool(p.entries.slice(0, 1), p.references)
    with pytest.raises(ValueError, match='snapshot changed'):
        TableSession(changed, path, reviewer='hieu')


def test_multiple_doi_values_remain_visible_without_selecting_one(tmp_path):
    s = TableSession(pool(), tmp_path / 'p.parquet', reviewer='hieu',
        papers=pa.table({'arxiv_id': ['r1', 'r1'], 'doi': ['10.1234/a', '10.1234/b']}))
    html = candidate_cells_html(s, s.candidates['p'][0])
    assert '10.1234/a' in html and '10.1234/b' in html and 'DOI cần kiểm tra' in html
    assert not s.labels


def test_widget_buttons_filter_and_autosave_callback(tmp_path):
    pytest.importorskip('ipywidgets')
    ui = LabelingTable(TableSession(pool(), tmp_path / 'p.parquet', reviewer='hieu'), page_size=1)
    try:
        ui.search.value = 'Other'
        no_match = next(b for b in ui.actions.children if getattr(b, 'description', '') == 'Không có match')
        assert no_match.disabled
        old_button = ui.table.children[2].children[0]
        old_button.click()
        assert ui.session.labels[('p', 'a')]['groundtruth_arxiv_id'] == 'r2'
        assert ui.session.key == ('p', 'b')
        old_button.click()  # stale rendered button cannot label the next entry
        assert ('p', 'b') not in ui.session.labels
        skip = next(b for b in ui.actions.children if getattr(b, 'description', '') == 'Chưa chắc / Tiếp')
        skip.click()
        assert ('p', 'b') not in ui.session.labels
        assert ui.session.key == ('q', 'c')
        no_match = next(b for b in ui.actions.children if getattr(b, 'description', '') == 'Không có match')
        no_match.click()
        assert ui.session.labels[('q', 'c')]['label_status'] == 'no_match'
    finally:
        ui.close()


def test_failed_widget_save_keeps_entry_and_last_valid_file(tmp_path, monkeypatch):
    pytest.importorskip('ipywidgets')
    s = TableSession(pool(), tmp_path / 'p.parquet', reviewer='hieu')
    s.save('r1')
    original = s.progress.read_bytes()
    ui = LabelingTable(s)
    def fail(*args, **kwargs):
        raise OSError('Drive unavailable')
    monkeypatch.setattr('matching.labeling.os.replace', fail)
    try:
        ui.table.children[2].children[0].click()
        assert ui.session.key == ('p', 'a')
        assert s.progress.read_bytes() == original
        assert 'Drive unavailable' in ui.status.value
        assert not s.progress.with_name(s.progress.name + '.lock').exists()
    finally:
        ui.close()
