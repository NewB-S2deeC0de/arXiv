"""Prepare a reproducible, provisional annotation plan from a recovered snapshot."""

from collections import Counter, defaultdict
import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from .candidate_pool import build_candidate_pool
from .labeling import pool_fingerprint


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
