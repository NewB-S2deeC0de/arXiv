from dataclasses import replace
import json
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.auto_matcher import write_auto_labels
from matching.candidate_pool import build_candidate_pool
from matching.groundtruth import prepare_groundtruth, split_publications, write_groundtruth
from matching.labeling import _save_labels, pool_fingerprint


@pytest.fixture
def corpus(tmp_path):
    manual_ids = [f'm{index:02}' for index in range(15)]
    auto_ids = [f'a{index:02}' for index in range(6)]
    entries = [
        dict(arxiv_id=pub, citation_key=f'key{index:02}')
        for pub in manual_ids + auto_ids
        for index in range(21 if pub.startswith('m') else 2)
    ]
    references = [dict(arxiv_id=pub, ref_arxiv_id=f'ref-{pub}') for pub in manual_ids + auto_ids]
    pool = build_candidate_pool(pa.Table.from_pylist(entries), pa.Table.from_pylist(references))
    fingerprint = pool_fingerprint(pool)
    manual = []
    for member in range(3):
        path = tmp_path / f'manual{member}.parquet'
        rows = {}
        for pub in manual_ids[member * 5:(member + 1) * 5]:
            for index in range(20):
                key = (pub, f'key{index:02}')
                rows[key] = dict(arxiv_id=pub, citation_key=key[1], groundtruth_arxiv_id=None,
                    label_status='no_match', reviewer=f'member{member}', updated_at='2026-11-23')
        _save_labels(path, rows, fingerprint)
        manual.append(path)
    auto = tmp_path / 'auto'
    write_auto_labels(pool, auto, publications=auto_ids)
    return dict(pool=pool, publication_universe=manual_ids + auto_ids,
                manual_paths=manual, auto_runs=[auto])


def issue_codes(result):
    return {issue['code'] for issue in result.report['issues']}


def edit_manual(path, transform):
    table = pq.read_table(path)
    rows = table.to_pylist()
    transform(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)


def edit_auto(directory, transform):
    path = directory / 'auto_labels.parquet'
    table = pq.read_table(path)
    rows = table.to_pylist()
    transform(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)


def test_three_member_groundtruth_roundtrip_and_publication_split(corpus, tmp_path):
    result = prepare_groundtruth(**corpus)
    assert result.report['status'] == 'ready'
    assert result.groundtruth.num_rows == 312
    assert result.groundtruth.column_names == [
        'arxiv_id', 'citation_key', 'groundtruth_arxiv_id', 'partition']
    assert pa.types.is_dictionary(result.groundtruth.schema.field('partition').type)
    split = result.publication_split.to_pylist()
    for part in ('test', 'valid'):
        assert sum(row['partition'] == part and row['label_pool'] == 'manual' for row in split) == 3
        assert sum(row['partition'] == part and row['label_pool'] == 'auto' for row in split) == 3
    assert sum(row['partition'] == 'train' for row in split) == 9
    mapping = {row['arxiv_id']: row['partition'] for row in split}
    for row in result.groundtruth.to_pylist():
        assert row['partition'] == mapping[row['arxiv_id']]
        assert row['citation_key'] != 'key20'
    write_groundtruth(result, tmp_path / 'out')
    assert pq.read_table(tmp_path / 'out/groundtruth.parquet').to_pylist() == result.groundtruth.to_pylist()
    assert (tmp_path / 'out/_SUCCESS').is_file()


def test_split_is_order_independent_and_seeded():
    manual = [f'm{i}' for i in range(15)]
    auto = [f'a{i}' for i in range(8)]
    first = split_publications(manual, auto)
    assert first == split_publications(reversed(manual), reversed(auto))
    assert first != split_publications(manual, auto, seed=43)
    with pytest.raises(ValueError):
        split_publications(manual, ['m0', *auto])


def test_duplicate_agreement_preserves_audit_without_inflating_counts(corpus, tmp_path):
    copy = tmp_path / 'copy.parquet'
    shutil.copyfile(corpus['manual_paths'][0], copy)
    result = prepare_groundtruth(**{**corpus, 'manual_paths': [copy, *corpus['manual_paths']]})
    assert result.report['status'] == 'ready'
    assert result.report['duplicate_agreements'] == 100
    assert result.groundtruth.num_rows == 312
    assert result.audit.num_rows == 412


def test_conflicting_manual_labels_block_export(corpus, tmp_path):
    copy = tmp_path / 'conflict.parquet'
    shutil.copyfile(corpus['manual_paths'][0], copy)
    def change(rows):
        rows[0].update(groundtruth_arxiv_id='ref-m00', label_status='matched')
    edit_manual(copy, change)
    result = prepare_groundtruth(**{**corpus, 'manual_paths': [copy, *corpus['manual_paths']]})
    assert 'conflicting_labels' in issue_codes(result)
    assert result.groundtruth is None
    out = tmp_path / 'blocked'
    write_groundtruth(result, out)
    assert not (out / 'groundtruth.parquet').exists()
    assert not (out / '_SUCCESS').exists()
    assert (out / 'label_audit.parquet').exists()
    assert json.loads((out / 'validation_report.json').read_text(encoding='utf-8'))['status'] == 'blocked'


def test_incomplete_auto_publication_is_blocked(corpus):
    edit_auto(corpus['auto_runs'][0], lambda rows: rows.pop())
    assert 'auto_incomplete' in issue_codes(prepare_groundtruth(**corpus))


def test_review_stays_unlabeled_until_explicit_manual_resolution(corpus, tmp_path):
    def pending(rows):
        rows[0].update(label_status='needs_review', groundtruth_arxiv_id=None)
    edit_auto(corpus['auto_runs'][0], pending)
    blocked = prepare_groundtruth(**corpus)
    assert 'unresolved_review' in issue_codes(blocked)
    path = tmp_path / 'review.parquet'
    row = dict(arxiv_id='a00', citation_key='key00', groundtruth_arxiv_id='ref-a00',
               label_status='matched', reviewer='member0', updated_at='2026-11-23')
    _save_labels(path, {('a00', 'key00'): row}, pool_fingerprint(corpus['pool']))
    result = prepare_groundtruth(**corpus, review_paths=[path])
    assert result.report['status'] == 'ready'
    assert result.report['resolved_review_entries'] == 1
    split = {row['arxiv_id']: row for row in result.publication_split.to_pylist()}
    assert split['a00']['label_pool'] == 'auto'


def test_review_cannot_override_an_existing_auto_decision(corpus, tmp_path):
    path = tmp_path / 'review.parquet'
    row = dict(arxiv_id='a00', citation_key='key00', groundtruth_arxiv_id='ref-a00',
               label_status='matched', reviewer='member0', updated_at='2026-11-23')
    _save_labels(path, {('a00', 'key00'): row}, pool_fingerprint(corpus['pool']))
    assert 'invalid_review_target' in issue_codes(prepare_groundtruth(**corpus, review_paths=[path]))


def test_auto_quota_uses_whole_corpus_not_only_labeled_publications(corpus):
    universe = corpus['publication_universe'] + [f'unlabeled{i}' for i in range(100)]
    result = prepare_groundtruth(**{**corpus, 'publication_universe': universe})
    assert result.report['required_auto_publications'] == 11
    assert 'auto_publication_quota' in issue_codes(result)


def test_manual_per_publication_and_per_member_requirements(corpus):
    edit_manual(corpus['manual_paths'][0], lambda rows: rows.pop(0))
    assert 'manual_below_20_entries' in issue_codes(prepare_groundtruth(**corpus))
    for path in corpus['manual_paths']:
        def one_member(rows):
            for row in rows:
                row['reviewer'] = 'member0'
        edit_manual(path, one_member)
    assert 'manual_owner_quota' in issue_codes(prepare_groundtruth(**corpus))


def test_snapshot_mismatch_is_rejected(corpus):
    pool = corpus['pool']
    entries = pool.entries.set_column(
        pool.entries.schema.get_field_index('title'), 'title',
        pa.array(['changed'] * pool.entries.num_rows))
    with pytest.raises(ValueError, match='snapshot changed'):
        prepare_groundtruth(**{**corpus, 'pool': replace(pool, entries=entries)})


def test_multiple_manual_reviewers_need_explicit_owner(corpus, tmp_path):
    path = tmp_path / 'double_review.parquet'
    shutil.copyfile(corpus['manual_paths'][0], path)
    def reviewer(rows):
        for row in rows:
            row['reviewer'] = 'second_reviewer'
    edit_manual(path, reviewer)
    args = {**corpus, 'manual_paths': [*corpus['manual_paths'], path]}
    assert 'manual_owner_required' in issue_codes(prepare_groundtruth(**args))
    owners = {f'm{i:02}': 'member0' for i in range(5)}
    assert prepare_groundtruth(**args, manual_owners=owners).report['status'] == 'ready'


def test_invalid_reference_target_cannot_enter_groundtruth(corpus):
    def invalid(rows):
        rows[0].update(label_status='matched', groundtruth_arxiv_id='outside-publication')
    edit_auto(corpus['auto_runs'][0], invalid)
    with pytest.raises(ValueError, match='không hợp lệ'):
        prepare_groundtruth(**corpus)


def test_export_preserves_existing_progress_and_refuses_overwrite(corpus, tmp_path):
    output = tmp_path / 'matching'
    output.mkdir()
    (output / 'manual-progress.txt').write_text('keep', encoding='utf-8')
    result = prepare_groundtruth(**corpus)
    write_groundtruth(result, output)
    original = (output / 'groundtruth.parquet').read_bytes()
    with pytest.raises(FileExistsError):
        write_groundtruth(result, output)
    assert (output / 'groundtruth.parquet').read_bytes() == original
    assert (output / 'manual-progress.txt').read_text(encoding='utf-8') == 'keep'


def test_manual_and_auto_publication_pools_cannot_overlap(corpus):
    auto = corpus['auto_runs'][0]
    # Generate an otherwise legitimate second auto run containing manual publications.
    extra = auto.parent / 'overlap'
    write_auto_labels(corpus['pool'], extra, publications=['m00'])
    result = prepare_groundtruth(**{**corpus, 'auto_runs': [auto, extra]})
    assert 'mixed_label_pools' in issue_codes(result)


def test_auto_snapshot_mismatch_is_rejected(corpus):
    path = corpus['auto_runs'][0] / 'summary.json'
    report = json.loads(path.read_text(encoding='utf-8'))
    report['candidate_pool_sha256'] = 'stale'
    path.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError, match='snapshot khác'):
        prepare_groundtruth(**corpus)
