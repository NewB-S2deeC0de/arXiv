import copy
import json

import pyarrow as pa
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.dataset_plan import prepare_dataset_plan, personal_manual_plan


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


def test_personal_plan_needs_no_group_corpus_or_auto_pool_and_is_reused(tmp_path):
    pool, _, _, _ = inputs()
    path = tmp_path / 'plans/member4.json'
    plan = personal_manual_plan(pool, path, member='member4', reviewer='fourth')
    original = path.read_bytes()
    assert plan['scope'] == 'personal_manual_only'
    assert plan['group_members'] == 4 and not plan['corpus_complete']
    assert len(plan['manual_assignments']) == 5
    assert sum(len(a['citation_keys']) for a in plan['manual_assignments']) == 100
    assert all(a['owner'] == 'member4' for a in plan['manual_assignments'])
    assert 'auto_publications' not in plan and plan['warnings']
    assert personal_manual_plan(pool, path, member='member4', reviewer='fourth') == plan
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match='Cấu hình chọn bài khác'):
        personal_manual_plan(pool, path, member='member4', reviewer='fourth', seed=43)
    assert path.read_bytes() == original


def test_personal_plan_does_not_resample_legacy_progress(tmp_path):
    pool, _, _, _ = inputs()
    old_progress = tmp_path / 'member1.parquet'
    old_progress.write_bytes(b'legacy placeholder; do not touch')
    path = tmp_path / 'plans/member1.json'
    with pytest.raises(ValueError, match='PLAN_MODE=existing'):
        personal_manual_plan(pool, path, member='member1', reviewer='hieu', progress=old_progress)
    assert not path.exists() and old_progress.read_bytes().startswith(b'legacy')


def test_personal_quality_filters_and_explicit_exclusions_are_honored(tmp_path):
    pool, _, checkpoint, snapshots = inputs()
    for item in checkpoint['processed'].values():
        item['stats']['bibtex_after'] = 23
    checkpoint['processed']['p000']['stats']['versions_failed'] = 1
    rows = snapshots.to_pylist()
    rows[1]['status'] = 'failed'
    plan = personal_manual_plan(pool, tmp_path / 'plan.json', member='member1', reviewer='hieu',
        checkpoint=checkpoint, snapshots=pa.Table.from_pylist(rows), excluded_publications=['p002'])
    rejected = {x['arxiv_id'] for x in plan['exclusions']}
    assert {'p000', 'p001', 'p002'} <= rejected
    assert not rejected & {a['arxiv_id'] for a in plan['manual_assignments']}
    assert not plan['warnings']


def test_personal_plan_supports_five_explicit_publications_and_detects_snapshot_change(tmp_path):
    pool, _, _, _ = inputs()
    chosen = ['p001', 'p002', 'p003', 'p004', 'p005']
    path = tmp_path / 'plan.json'
    plan = personal_manual_plan(pool, path, member='member2', reviewer='anh', publications=chosen)
    assert {a['arxiv_id'] for a in plan['manual_assignments']} == set(chosen)
    changed = build_candidate_pool(pool.entries.slice(1), pool.references)
    with pytest.raises(ValueError, match='snapshot khác'):
        personal_manual_plan(changed, path, member='member2', reviewer='anh', publications=chosen)


def test_personal_plan_refuses_fewer_than_five_eligible_publications(tmp_path):
    pool, _, _, _ = inputs()
    subset = build_candidate_pool(pool.entries.slice(0, 23 * 4), pool.references)
    with pytest.raises(ValueError, match='cần 5'):
        personal_manual_plan(subset, tmp_path / 'plan.json', member='member3', reviewer='kien')
    assert not (tmp_path / 'plan.json').exists()


def test_personal_plan_does_not_silently_accept_damaged_saved_assignments(tmp_path):
    pool, _, _, _ = inputs()
    path = tmp_path / 'plan.json'
    plan = personal_manual_plan(pool, path, member='member1', reviewer='hieu')
    plan['manual_assignments'][0]['citation_keys'] = ['not-an-entry'] * 20
    path.write_text(json.dumps(plan), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='không đúng phân công'):
        personal_manual_plan(pool, path, member='member1', reviewer='hieu')
    assert path.read_bytes() == before
