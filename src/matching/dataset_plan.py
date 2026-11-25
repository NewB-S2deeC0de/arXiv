"""Prepare a reproducible, provisional annotation plan from a recovered snapshot."""

from collections import Counter, defaultdict
import argparse
import hashlib
import json
from pathlib import Path
import os
import tempfile

import pyarrow.parquet as pq

from .candidate_pool import build_candidate_pool
from .labeling import pool_fingerprint, _exclusive_progress


def _write_plan(path, plan):
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def personal_manual_plan(pool, path, *, member, reviewer, seed=42, group_members=4,
                         publications=None, excluded_publications=(), checkpoint=None,
                         snapshots=None, progress=None):
    """Persist five personal publications without needing the other members' data.

    Existing plans are never resampled; legacy progress requires its original
    plan. Optional quality artifacts restrict selection but do not certify the
    snapshot or make local quotas equivalent to group acceptance.
    """
    if not member or not reviewer or type(seed) is not int:
        raise ValueError('Cần mã thành viên, người gán nhãn và seed nguyên')
    if type(group_members) is not int or group_members < 1:
        raise ValueError('Số thành viên nhóm phải là số nguyên dương')
    if publications is not None and (len(publications) != 5 or len(set(publications)) != 5):
        raise ValueError('MANUAL_PUBLICATIONS phải gồm đúng 5 ID khác nhau')
    requested = None if publications is None else sorted(set(publications))
    excluded = sorted(set(excluded_publications))
    selection = dict(seed=seed, publications=requested, excluded_publications=excluded)
    fingerprint = pool_fingerprint(pool)
    path = Path(path)
    with _exclusive_progress(path):
        if path.exists():
            plan = json.loads(path.read_text(encoding='utf-8'))
            if plan.get('candidate_pool_sha256') != fingerprint:
                raise ValueError('Kế hoạch cá nhân thuộc snapshot khác; không ghi đè kế hoạch/nhãn cũ')
            if plan.get('member') != member or plan.get('selection_request') != selection:
                raise ValueError('Cấu hình chọn bài khác kế hoạch đã lưu; dùng lại cấu hình cũ')
            assignments = plan.get('manual_assignments', [])
            known = {(r['arxiv_id'], r['citation_key']) for r in pool.entries.to_pylist()}
            if (plan.get('scope') != 'personal_manual_only' or len(assignments) != 5 or
                len({a['arxiv_id'] for a in assignments}) != 5 or
                any(a.get('owner') != member or len(a.get('citation_keys', [])) < 20 or
                    len(set(a['citation_keys'])) != len(a['citation_keys']) or
                    any((a['arxiv_id'], k) not in known for k in a['citation_keys'])
                    for a in assignments)):
                raise ValueError('Kế hoạch cá nhân đã lưu không đúng phân công 5 bài × ít nhất 20 entry')
            return plan
        if progress is not None and Path(progress).exists():
            raise ValueError('Đã có nhãn cũ nhưng chưa có kế hoạch cá nhân. Chọn PLAN_MODE=existing '
                             'và điền PLAN_PATH gốc để tiếp tục; không chọn lại 5 bài')
        summary = {r['arxiv_id']: r for r in pool.summary.to_pylist()}
        if requested is not None and (len(requested) != 5 or set(requested) - set(summary)):
            raise ValueError('MANUAL_PUBLICATIONS phải gồm đúng 5 ID khác nhau có trong BibTeX')
        rows_by_pub = defaultdict(list)
        if snapshots is not None:
            for row in snapshots.to_pylist():
                if row.get('scope') == 'version_query':
                    rows_by_pub[row['arxiv_id']].append(row)
        eligible, rejected = [], []
        for pub, item in sorted(summary.items()):
            reasons = []
            if item['bibtex_entries'] < 20:
                reasons.append('fewer_than_20_entries')
            if not item['candidate_references']:
                reasons.append('no_arxiv_candidates')
            if pub in excluded:
                reasons.append('explicitly_excluded')
            stats = None
            if checkpoint is not None:
                stats = checkpoint.get('processed', {}).get(pub, {}).get('stats')
                if not stats:
                    reasons.append('missing_checkpoint_publication')
                else:
                    if stats.get('bibtex_after') != item['bibtex_entries']:
                        raise ValueError(f'BibTeX khác số dòng checkpoint: {pub}')
                    if any(stats.get(k, 0) for k in ('versions_failed', 'versions_partial',
                           'versions_pending', 'missing', 'bibtex_conflicts', 'bibtex_unresolved',
                           'bibtex_unexportable')):
                        reasons.append('incomplete_or_conflicting_m2')
                    total = stats.get('versions_total', 0)
                    if (total < 1 or stats.get('versions_parsed', 0) < 1 or
                        stats.get('versions_parsed', 0) + stats.get('versions_empty', 0) != total):
                        reasons.append('unaccounted_or_unparsed_versions')
            if snapshots is not None:
                snapshots_for_pub = rows_by_pub[pub]
                versions = [r['version'] for r in snapshots_for_pub]
                if (not versions or len(versions) != len(set(versions)) or
                    any(r.get('status') != 'succeeded' or r.get('reference_count') is None
                        for r in snapshots_for_pub) or
                    (stats is not None and set(versions) != set(range(1, stats.get('versions_total', 0) + 1)))):
                    reasons.append('incomplete_reference_snapshots')
            if reasons:
                rejected.append(dict(arxiv_id=pub, reasons=reasons))
            else:
                eligible.append(pub)
        if requested is not None:
            if set(requested) - set(eligible):
                raise ValueError('Bài chọn thủ công không đạt điều kiện: ' + ', '.join(sorted(set(requested) - set(eligible))))
            chosen = requested
        else:
            if len(eligible) < 5:
                raise ValueError(f'Chỉ có {len(eligible)} bài đủ điều kiện, cần 5. Kiểm tra dữ liệu và báo cáo M1/M2')
            chosen = sorted(eligible, key=lambda p: _rank(seed, 'personal:' + member, p))[:5]
        keys = defaultdict(list)
        for row in pool.entries.to_pylist():
            if row['arxiv_id'] in chosen:
                keys[row['arxiv_id']].append(row['citation_key'])
        plan = dict(schema_version=2, scope='personal_manual_only', corpus_complete=False,
            candidate_pool_sha256=fingerprint, seed=seed, member=member, reviewer=reviewer,
            group_members=group_members, selection_request=selection,
            manual_assignments=[dict(arxiv_id=pub, owner=member, label_status='pending',
                citation_keys=sorted(keys[pub], key=lambda k: _rank(seed, f'entry:{pub}', k))[:20],
                bibtex_entries=summary[pub]['bibtex_entries'],
                candidate_references=summary[pub]['candidate_references']) for pub in chosen],
            exclusions=rejected, quality_artifacts=dict(checkpoint=checkpoint is not None,
                reference_snapshots=snapshots is not None),
            warnings=[message for condition, message in [
                (checkpoint is None, 'Chưa kiểm tra chất lượng/độ đầy đủ M2 vì không cung cấp checkpoint'),
                (snapshots is None, 'Chưa xác minh snapshot references; candidate rỗng/thiếu không phải nhãn âm'),
                (checkpoint is None or snapshots is None, 'Lựa chọn tạm dựa trên bảng hiện có; chưa thay nghiệm thu dữ liệu')]
                if condition],
            outstanding=['human_labels', 'group_publication_overlap_check', 'group_quota_and_partition'])
        _write_plan(path, plan)
        return plan


def _rank(seed, scope, value):
    return hashlib.sha256(f'{seed}|{scope}|{value}'.encode('utf-8')).hexdigest(), value


def prepare_dataset_plan(pool, papers, checkpoint, snapshots, *, members=3, seed=42,
                         corpus_complete=False):
    """Select before matching; incomplete inputs stay visible in an exclusion audit.

    This prepares work, not labels or a final train/valid/test split. Empty but
    successful S2 snapshots are valid. Manual work uses nonempty candidate pools.
    Missing optional BibTeX fields are reported without filtering easy examples.
    """
    if type(members) is not int or members < 1 or type(seed) is not int:
        raise ValueError('members must be positive and seed must be an integer')
    universe_list = papers['arxiv_id'].to_pylist()
    universe = set(universe_list)
    if len(universe) != len(universe_list) or any(not x for x in universe):
        raise ValueError('papers must contain unique, nonempty publication IDs')
    processed = checkpoint['processed']
    if set(processed) != universe:
        raise ValueError('M1 papers and M2 checkpoint publication IDs differ')
    summary = {r['arxiv_id']: r for r in pool.summary.to_pylist()}
    if set(summary) - universe or set(pool.references['arxiv_id'].to_pylist()) - universe:
        raise ValueError('Candidate pool contains publications outside papers')
    version_rows = defaultdict(list)
    for row in snapshots.to_pylist():
        if row['arxiv_id'] not in universe:
            raise ValueError('Reference snapshot publication outside papers')
        if row['scope'] == 'version_query':
            version_rows[row['arxiv_id']].append(row)
    eligible, exclusions = [], []
    for pub in sorted(universe):
        reasons = []
        stats = processed[pub]['stats']
        observed = summary.get(pub, {}).get('bibtex_entries', 0)
        if 'bibtex_after' in stats and observed != stats['bibtex_after']:
            raise ValueError(f'M2 BibTeX count differs from checkpoint: {pub}')
        total = stats.get('versions_total', 0)
        if pub not in summary:
            reasons.append('no_exported_bibtex')
        if any(stats.get(k, 0) for k in
               ('versions_failed', 'versions_partial', 'versions_pending', 'missing',
                'bibtex_unresolved', 'bibtex_unexportable', 'bibtex_conflicts')):
            reasons.append('incomplete_or_conflicting_m2')
        if (total < 1 or stats.get('versions_parsed', 0) < 1 or
                stats.get('versions_parsed', 0) + stats.get('versions_empty', 0) != total):
            reasons.append('unaccounted_or_unparsed_versions')
        rows = version_rows[pub]
        versions = [r['version'] for r in rows]
        if (set(versions) != set(range(1, total + 1)) or len(versions) != total or
                any(r['status'] != 'succeeded' or r['reference_count'] is None
                    for r in rows)):
            reasons.append('incomplete_reference_snapshots')
        if reasons:
            exclusions.append(dict(arxiv_id=pub, reasons=reasons))
        else:
            eligible.append(pub)
    manual_candidates = [p for p in eligible if summary[p]['bibtex_entries'] >= 20
                         and summary[p]['candidate_references'] > 0]
    manual_candidates.sort(key=lambda p: _rank(seed, 'manual', p))
    manual_count = members * 5
    if len(manual_candidates) < manual_count:
        raise ValueError('Not enough complete publications for manual assignment')
    manual = manual_candidates[:manual_count]
    auto_candidates = sorted(set(eligible) - set(manual),
                             key=lambda p: _rank(seed, 'auto', p))
    auto_required = max(2 * members, (len(universe) - manual_count + 9) // 10)
    if len(auto_candidates) < auto_required:
        raise ValueError('Not enough complete publications for provisional auto quota')
    entries = pool.entries.to_pylist()
    by_pub = defaultdict(list)
    for row in entries:
        by_pub[row['arxiv_id']].append(row['citation_key'])
    assignments = []
    for i, pub in enumerate(manual):
        keys = sorted(by_pub[pub], key=lambda k: _rank(seed, f'entry:{pub}', k))[:20]
        assignments.append(dict(arxiv_id=pub, owner=f'member{i // 5 + 1}',
            citation_keys=keys, label_status='pending', **{
                k: summary[pub][k] for k in ('bibtex_entries', 'candidate_references')}))
    return dict(
        schema_version=1, candidate_pool_sha256=pool_fingerprint(pool), seed=seed,
        scope='complete_corpus' if corpus_complete else 'provisional_range',
        corpus_complete=corpus_complete, publications=len(universe),
        eligible_publications=len(eligible), manual_assignments=assignments,
        auto_publications=auto_candidates[:auto_required],
        provisional_auto_required=auto_required, exclusions=exclusions,
        exclusion_reason_counts=dict(Counter(reason for r in exclusions for reason in r['reasons'])),
        missing_entry_fields={k: sum(not row[k] for row in entries)
                              for k in ('title', 'authors', 'year', 'doi')},
        selection_policy={
            'ranking': 'SHA256(seed|scope|ID); no heuristic results used for selection',
            'manual': '5 publications/member; 20 sampled entries/publication; nonempty candidates',
            'auto': 'all entries of selected publications, including valid empty candidate pools',
            'eligibility': 'all versions accounted; M2 no failures/conflicts; S2 snapshots successful',
            'reference_scope': 'M1 deduplicated paper union; not a verified per-version bibliography',
        },
        outstanding=['human_manual_labels', 'resolve_auto_needs_review',
                     'merge_remaining_group_ranges_and_recompute_quota', 'final_partition_and_export'],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True,
                        help='Root containing m1 and m2 input subdirectories')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--members', type=int, default=3)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    root = args.input
    manifest = json.loads((root / 'm1/manifest.json').read_text(encoding='utf-8'))
    checksums = {}
    for name in ('papers.parquet', 'references.parquet', 'reference_snapshots.parquet'):
        with (root / 'm1' / name).open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != manifest['checksums'][name]:
            raise ValueError(f'M1 checksum mismatch: {name}')
        checksums[name] = digest
    pool = build_candidate_pool(root / 'm2/bibtex.parquet', root / 'm1/references.parquet')
    plan = prepare_dataset_plan(pool, pq.read_table(root / 'm1/papers.parquet'),
        json.loads((root / 'm2/checkpoint.json').read_text(encoding='utf-8')),
        pq.read_table(root / 'm1/reference_snapshots.parquet'), members=args.members, seed=args.seed)
    plan['verified_m1_checksums'] = checksums
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in plan.items()
                      if k not in ('manual_assignments', 'auto_publications', 'exclusions')},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
