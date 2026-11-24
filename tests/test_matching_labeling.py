import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.labeling import run_labeling_session


def make_pool():
    return build_candidate_pool(
        pa.Table.from_pylist([
            {'arxiv_id': 'p', 'citation_key': 'a', 'title': 'Entry A',
             'authors': ['Jane Doe'], 'year': 2020},
            {'arxiv_id': 'p', 'citation_key': 'b', 'title': 'Entry B'},
            {'arxiv_id': 'q', 'citation_key': 'c', 'title': 'No candidates'},
        ]),
        pa.Table.from_pylist([
            {'arxiv_id': 'p', 'ref_arxiv_id': 'r1', 'ref_title': 'Candidate One',
             'ref_authors': ['Jane Doe'], 'ref_submission_date': '2020-01-01'},
            {'arxiv_id': 'p', 'ref_arxiv_id': 'r2', 'ref_title': 'Candidate Two'},
        ]),
    )


def run(pool, path, choices, **kwargs):
    commands = iter(choices)
    messages = []
    result = run_labeling_session(pool, path, reviewer='reviewer',
        input_fn=lambda _: next(commands), output=messages.append, **kwargs)
    return result.to_pylist(), '\n'.join(messages)


def test_notebook_session_saves_match_no_match_and_keeps_skip_unlabeled(tmp_path):
    path = tmp_path / 'labels.parquet'
    labels, display = run(make_pool(), path, ['bad', '99', '2', 's', 'n'])
    assert [(row['citation_key'], row['groundtruth_arxiv_id'], row['label_status'])
            for row in labels] == [('a', 'r2', 'matched'), ('c', None, 'no_match')]
    assert pq.read_table(path).to_pylist() == labels
    for value in ('Entry A', 'Jane Doe', '2020', 'Candidate One', 'Candidate Two', '[thiếu dữ liệu]'):
        assert value in display
    assert not path.with_name(path.name + '.lock').exists()


def test_resume_preserves_previous_decisions_and_allows_correction(tmp_path):
    path = tmp_path / 'labels.parquet'
    run(make_pool(), path, ['1', 'q'])
    labels, _ = run(make_pool(), path, ['p', '2', 'n', 'q'])
    assert [(row['citation_key'], row['groundtruth_arxiv_id'])
            for row in labels] == [('a', 'r2'), ('b', None)]


def test_publication_filter_retains_other_publications_and_zero_candidate_choice(tmp_path):
    path = tmp_path / 'labels.parquet'
    run(make_pool(), path, ['n'], publications=['q'])
    labels, _ = run(make_pool(), path, ['1', 'q'], publications=['p'])
    assert {row['arxiv_id'] for row in labels} == {'p', 'q'}


def test_clear_does_not_turn_unlabeled_into_no_match(tmp_path):
    path = tmp_path / 'labels.parquet'
    run(make_pool(), path, ['1', 'q'])
    labels, _ = run(make_pool(), path, ['p', 'c', 'q'])
    assert labels == []
    assert pq.read_table(path).num_rows == 0


def test_snapshot_change_cannot_silently_reuse_labels(tmp_path):
    path = tmp_path / 'labels.parquet'
    pool = make_pool()
    run(pool, path, ['1', 'q'])
    changed = build_candidate_pool(
        pool.entries.set_column(pool.entries.schema.get_field_index('title'),
                                'title', pa.array(['Changed', 'Entry B', 'No candidates'])),
        pool.references)
    with pytest.raises(ValueError, match='snapshot changed'):
        run(changed, path, ['q'])
    assert not path.with_name(path.name + '.lock').exists()


def test_concurrent_session_is_rejected(tmp_path):
    path = tmp_path / 'labels.parquet'
    path.with_name(path.name + '.lock').touch()
    with pytest.raises(RuntimeError, match='locked'):
        run(make_pool(), path, ['q'])


def test_entry_plan_limits_ui_without_losing_previously_saved_labels(tmp_path):
    path = tmp_path / 'labels.parquet'
    run(make_pool(), path, ['n'], entry_keys=[('q', 'c')])
    labels, display = run(make_pool(), path, ['1'], entry_keys=[('p', 'b')])
    assert {(r['arxiv_id'], r['citation_key']) for r in labels} == {('q', 'c'), ('p', 'b')}
    assert 'Entry A' not in display
    with pytest.raises(ValueError, match='unknown or excluded'):
        run(make_pool(), path, ['q'], publications=['q'], entry_keys=[('p', 'a')])


def test_failed_save_keeps_last_valid_progress(tmp_path, monkeypatch):
    path = tmp_path / 'labels.parquet'
    run(make_pool(), path, ['1', 'q'])
    original = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Simulated write failure')
    monkeypatch.setattr('matching.labeling.os.replace', fail)
    with pytest.raises(OSError, match='Simulated'):
        run(make_pool(), path, ['n'])
    assert path.read_bytes() == original
    assert not path.with_name(path.name + '.lock').exists()
    assert not list(tmp_path.glob('*.tmp'))
