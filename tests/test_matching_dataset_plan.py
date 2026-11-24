import copy

import pyarrow as pa
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.dataset_plan import prepare_dataset_plan


def inputs():
    pubs = [f'p{i:03}' for i in range(80)]
    pool = build_candidate_pool(pa.Table.from_pylist([
        dict(arxiv_id=p, citation_key=f'k{k}', title='Example')
        for p in pubs for k in range(23)]), pa.Table.from_pylist([
        dict(arxiv_id=p, ref_arxiv_id='target') for p in pubs]))
    papers = pa.table({'arxiv_id': pubs})
    checkpoint = {'processed': {p: {'stats': dict(versions_total=1, versions_parsed=1)}
                                for p in pubs}}
    snapshots = pa.Table.from_pylist([dict(arxiv_id=p, version=1, scope='version_query',
        status='succeeded', reference_count=1) for p in pubs])
    return pool, papers, checkpoint, snapshots


def test_provisional_selection_is_reproducible_disjoint_and_balanced():
    args = inputs()
    plan = prepare_dataset_plan(*args)
    assert plan == prepare_dataset_plan(*args)
    assignments = plan['manual_assignments']
    assert len(assignments) == 15
    for member in ['member1', 'member2', 'member3']:
        assert sum(a['owner'] == member for a in assignments) == 5
    assert all(len(set(a['citation_keys'])) == 20 and a['label_status'] == 'pending'
               for a in assignments)
    assert len(plan['auto_publications']) == 7  # ceil((80 - 15) / 10)
    assert not set(plan['auto_publications']) & {a['arxiv_id'] for a in assignments}
    assert plan['corpus_complete'] is False
    assert 'final_partition_and_export' in plan['outstanding']


def test_failed_versions_conflicts_missing_and_duplicate_snapshots_are_excluded():
    pool, papers, checkpoint, snapshots = inputs()
    checkpoint['processed']['p000']['stats']['versions_failed'] = 1
    checkpoint['processed']['p001']['stats']['bibtex_conflicts'] = 1
    rows = snapshots.to_pylist()
    rows[2]['status'] = 'failed'
    rows.append(copy.deepcopy(rows[3]))
    plan = prepare_dataset_plan(pool, papers, checkpoint, pa.Table.from_pylist(rows))
    rejected = {r['arxiv_id']: r['reasons'] for r in plan['exclusions']}
    assert set(rejected) == {'p000', 'p001', 'p002', 'p003'}
    selected = set(plan['auto_publications']) | {a['arxiv_id'] for a in plan['manual_assignments']}
    assert not selected & set(rejected)
    assert plan['provisional_auto_required'] == 7  # denominator retains excluded papers


def test_different_m1_m2_universes_cannot_be_silently_joined():
    pool, papers, checkpoint, snapshots = inputs()
    del checkpoint['processed']['p000']
    with pytest.raises(ValueError, match='publication IDs differ'):
        prepare_dataset_plan(pool, papers, checkpoint, snapshots)


def test_truncated_bibliography_cannot_be_treated_as_complete_input():
    pool, papers, checkpoint, snapshots = inputs()
    checkpoint['processed']['p000']['stats']['bibtex_after'] = 24
    with pytest.raises(ValueError, match='count differs'):
        prepare_dataset_plan(pool, papers, checkpoint, snapshots)
