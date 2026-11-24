"""Hợp nhất nhãn, kiểm tra quota và chia tập theo publication."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .auto_matcher import AUTO_LABEL_SCHEMA
from .candidate_pool import _read_parquet, build_candidate_pool
from .labeling import _exclusive_progress, _load_labels, pool_fingerprint


PARTITION_TYPE = pa.dictionary(pa.int8(), pa.string())
GROUNDTRUTH_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('groundtruth_arxiv_id', pa.string()),
    pa.field('partition', PARTITION_TYPE, nullable=False),
])
AUDIT_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('groundtruth_arxiv_id', pa.string()),
    pa.field('label_status', pa.string(), nullable=False),
    pa.field('source_kind', pa.string(), nullable=False),
    pa.field('source_path', pa.string(), nullable=False),
    pa.field('reviewer', pa.string()),
    pa.field('reason', pa.string()),
])
SPLIT_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('label_pool', pa.string(), nullable=False),
    pa.field('owner', pa.string()),
    pa.field('partition', PARTITION_TYPE, nullable=False),
    pa.field('labeled_entries', pa.int64(), nullable=False),
    pa.field('bibtex_entries', pa.int64(), nullable=False),
])


def split_publications(manual, automatic, *, members=3, seed=42):
    """Xếp hạng bằng hash seed/ID để kết quả độc lập thứ tự file đầu vào."""
    if type(members) is not int or members < 1 or type(seed) is not int:
        raise ValueError('members phải nguyên dương và seed phải là số nguyên')
    manual, automatic = set(manual), set(automatic)
    if manual & automatic:
        raise ValueError('Publication manual và auto phải tách biệt')
    if len(manual) < 5 * members or len(automatic) < 2 * members:
        raise ValueError('Chưa đủ publication để chia tập theo số thành viên')
    result = {}
    for pool_name, publications in (('manual', manual), ('auto', automatic)):
        def rank(publication):
            value = json.dumps([seed, pool_name, publication], ensure_ascii=False)
            return hashlib.sha256(value.encode('utf-8')).hexdigest(), publication
        ordered = sorted(publications, key=rank)
        for index, publication in enumerate(ordered):
            partition = 'test' if index < members else 'valid' if index < 2 * members else 'train'
            result[publication] = partition
    return result


def _auto_rows(directory, fingerprint, entries, candidate_ids):
    directory = Path(directory)
    if not (directory / '_SUCCESS').is_file():
        raise ValueError(f'Auto output chưa hoàn tất: {directory}')
    report = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    if report.get('candidate_pool_sha256') != fingerprint:
        raise ValueError(f'Auto labels thuộc snapshot khác: {directory}')
    table = pq.ParquetFile(directory / 'auto_labels.parquet').read()
    if not table.schema.equals(AUTO_LABEL_SCHEMA):
        raise ValueError('Schema auto labels không hợp lệ')
    seen = set()
    for row in table.to_pylist():
        key = (row['arxiv_id'], row['citation_key'])
        target, status = row['groundtruth_arxiv_id'], row['label_status']
        if key not in entries or key in seen:
            raise ValueError(f'Khóa auto label không hợp lệ hoặc bị lặp: {key}')
        seen.add(key)
        valid = (
            status == 'matched' and target in candidate_ids.get(key[0], set())
        ) or (status in {'no_match', 'needs_review'} and target is None)
        if not valid:
            raise ValueError(f'Quyết định auto không hợp lệ: {key}')
        yield row


@dataclass(frozen=True)
class GroundtruthResult:
    groundtruth: pa.Table | None
    publication_split: pa.Table | None
    audit: pa.Table
    report: dict


def prepare_groundtruth(pool, *, publication_universe, manual_paths=(), auto_runs=(),
                        review_paths=(), members=3, seed=42, manual_owners=None):
    """Chặn output khi thiếu nhãn/quota, có review hoặc xung đột.

    publication_universe lấy từ papers M1 của toàn bộ corpus đã chốt.
    manual_owners chỉ cần khi một publication có nhiều người cùng gán nhãn.
    """
    if type(members) is not int or members < 1 or type(seed) is not int:
        raise ValueError('members phải nguyên dương và seed phải là số nguyên')
    universe = set(publication_universe)
    if not universe or any(not isinstance(pub, str) or not pub or pub != pub.strip() for pub in universe):
        raise ValueError('Cần danh sách publication hợp lệ của toàn corpus')
    entry_keys = {(row['arxiv_id'], row['citation_key']) for row in pool.entries.to_pylist()}
    if {key[0] for key in entry_keys} - universe:
        raise ValueError('Có BibTeX publication nằm ngoài papers corpus được cung cấp')
    candidate_ids = defaultdict(set)
    for row in pool.references.to_pylist():
        candidate_ids[row['arxiv_id']].add(row['ref_arxiv_id'])
    fingerprint = pool_fingerprint(pool)
    audit_rows = []
    for source_kind, paths in (('manual', manual_paths), ('auto_review', review_paths)):
        for path in sorted(set(map(Path, paths))):
            labels = _load_labels(path, fingerprint, entry_keys, candidate_ids)
            if not path.is_file():
                raise FileNotFoundError(path)
            for row in labels.values():
                audit_rows.append(dict(
                    arxiv_id=row['arxiv_id'], citation_key=row['citation_key'],
                    groundtruth_arxiv_id=row['groundtruth_arxiv_id'],
                    label_status=row['label_status'], source_kind=source_kind,
                    source_path=str(path), reviewer=row['reviewer'], reason=None))
    for directory in sorted(set(map(Path, auto_runs))):
        for row in _auto_rows(directory, fingerprint, entry_keys, candidate_ids):
            audit_rows.append(dict(
                arxiv_id=row['arxiv_id'], citation_key=row['citation_key'],
                groundtruth_arxiv_id=row['groundtruth_arxiv_id'],
                label_status=row['label_status'], source_kind='auto',
                source_path=str(directory), reviewer=None, reason=row['reason']))
    grouped = defaultdict(list)
    kinds = defaultdict(set)
    reviewers = defaultdict(set)
    for row in audit_rows:
        grouped[row['arxiv_id'], row['citation_key']].append(row)
        kinds[row['arxiv_id']].add(row['source_kind'])
        if row['reviewer']:
            reviewers[row['arxiv_id']].add(row['reviewer'])
    issues = []
    merged = {}
    resolved_reviews = 0
    for key, rows in sorted(grouped.items()):
        reviewed = [row for row in rows if row['source_kind'] == 'auto_review']
        effective = rows
        if reviewed:
            original = [row for row in rows if row['source_kind'] != 'auto_review']
            if not original or any(row['source_kind'] != 'auto' or row['label_status'] != 'needs_review'
                                   for row in original):
                issues.append(dict(code='invalid_review_target', arxiv_id=key[0], citation_key=key[1]))
                continue
            effective = reviewed
        decisions = {(row['label_status'], row['groundtruth_arxiv_id']) for row in effective}
        if len(decisions) > 1:
            issues.append(dict(code='conflicting_labels', arxiv_id=key[0], citation_key=key[1],
                               sources=sorted({row['source_path'] for row in rows})))
        elif effective[0]['label_status'] == 'needs_review':
            issues.append(dict(code='unresolved_review', arxiv_id=key[0], citation_key=key[1]))
        else:
            merged[key] = effective[0]['groundtruth_arxiv_id']
            resolved_reviews += bool(reviewed)
    manual = {pub for pub, sources in kinds.items() if 'manual' in sources}
    automatic = {pub for pub, sources in kinds.items() if 'auto' in sources}
    for publication in sorted(manual & automatic):
        issues.append(dict(code='mixed_label_pools', arxiv_id=publication))
    owners = {}
    if manual_owners is not None and not isinstance(manual_owners, dict):
        raise ValueError('manual_owners phải là object publication → reviewer')
    manual_owners = manual_owners or {}
    if set(manual_owners) - manual:
        raise ValueError('manual_owners chứa publication ngoài pool manual')
    for publication in sorted(manual):
        available = reviewers[publication]
        owner = manual_owners.get(publication)
        if owner is None and len(available) == 1:
            owner = next(iter(available))
        if owner not in available:
            issues.append(dict(code='manual_owner_required', arxiv_id=publication,
                               reviewers=sorted(available)))
        else:
            owners[publication] = owner
    entry_counts = Counter(key[0] for key in entry_keys)
    labeled_counts = Counter(key[0] for key in merged)
    for publication in sorted(manual):
        if labeled_counts[publication] < 20:
            issues.append(dict(code='manual_below_20_entries', arxiv_id=publication,
                               count=labeled_counts[publication]))
    for publication in sorted(automatic):
        if labeled_counts[publication] != entry_counts[publication]:
            issues.append(dict(code='auto_incomplete', arxiv_id=publication,
                labeled=labeled_counts[publication], expected=entry_counts[publication]))
    owner_counts = Counter(owners.values())
    required_auto = max(2 * members, (len(universe) - len(manual) + 9) // 10)
    if len(manual) < 5 * members:
        issues.append(dict(code='manual_publication_quota', actual=len(manual), required=5 * members))
    if sum(labeled_counts[pub] for pub in manual) < 100 * members:
        issues.append(dict(code='manual_entry_quota', required=100 * members))
    if len(owner_counts) != members or any(count < 5 for count in owner_counts.values()):
        issues.append(dict(code='manual_owner_quota', actual=dict(sorted(owner_counts.items())),
                           required_members=members, required_publications_per_member=5))
    if len(automatic) < required_auto:
        issues.append(dict(code='auto_publication_quota', actual=len(automatic), required=required_auto))
    audit_rows.sort(key=lambda row: (row['arxiv_id'], row['citation_key'],
                                    row['source_kind'], row['source_path']))
    report = dict(
        status='blocked' if issues else 'ready',
        candidate_pool_sha256=fingerprint, members=members, seed=seed,
        publication_universe=sorted(universe), population_size=len(universe),
        manual_publications=sorted(manual), auto_publications=sorted(automatic),
        required_auto_publications=required_auto, labeled_entries=len(merged),
        resolved_review_entries=resolved_reviews,
        duplicate_agreements=sum(len(rows) - 1 for rows in grouped.values()
            if len({(r['label_status'], r['groundtruth_arxiv_id']) for r in rows}) == 1),
        issues=issues,
    )
    audit = pa.Table.from_pylist(audit_rows, schema=AUDIT_SCHEMA)
    if issues:
        return GroundtruthResult(None, None, audit, report)
    partition = split_publications(manual, automatic, members=members, seed=seed)
    rows = [dict(arxiv_id=pub, citation_key=key, groundtruth_arxiv_id=target,
                 partition=partition[pub]) for (pub, key), target in sorted(merged.items())]
    split = [dict(arxiv_id=pub, label_pool='manual' if pub in manual else 'auto',
                  owner=owners.get(pub), partition=part, labeled_entries=labeled_counts[pub],
                  bibtex_entries=entry_counts[pub]) for pub, part in sorted(partition.items())]
    return GroundtruthResult(pa.Table.from_pylist(rows, schema=GROUNDTRUTH_SCHEMA),
                             pa.Table.from_pylist(split, schema=SPLIT_SCHEMA), audit, report)


def write_groundtruth(result, output):
    """Ghi report cả khi blocked; chỉ xuất groundtruth khi mọi kiểm tra đạt."""
    output = Path(output)
    reserved = ['label_audit.parquet', 'validation_report.json', 'groundtruth.parquet',
                'publication_split.parquet', '_SUCCESS']
    with _exclusive_progress(output / 'groundtruth.parquet'):
        for name in reserved:
            if (output / name).exists():
                raise FileExistsError(output / name)
        pq.write_table(result.audit, output / 'label_audit.parquet')
        (output / 'validation_report.json').write_text(
            json.dumps(result.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if result.groundtruth is not None:
            pq.write_table(result.groundtruth, output / 'groundtruth.parquet')
            pq.write_table(result.publication_split, output / 'publication_split.parquet')
            (output / '_SUCCESS').touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bibtex', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--papers', type=Path, required=True)
    parser.add_argument('--manual', type=Path, action='append', default=[])
    parser.add_argument('--auto', type=Path, action='append', default=[])
    parser.add_argument('--review', type=Path, action='append', default=[])
    parser.add_argument('--owners', type=Path)
    parser.add_argument('--members', type=int, default=3)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    pool = build_candidate_pool(args.bibtex, args.references)
    papers = _read_parquet(args.papers)
    if 'arxiv_id' not in papers.column_names:
        parser.error('papers thiếu cột arxiv_id')
    owners = json.loads(args.owners.read_text(encoding='utf-8')) if args.owners else None
    result = prepare_groundtruth(pool, publication_universe=papers['arxiv_id'].to_pylist(),
        manual_paths=args.manual, auto_runs=args.auto, review_paths=args.review, manual_owners=owners,
        members=args.members, seed=args.seed)
    write_groundtruth(result, args.out)
    print(json.dumps(result.report, ensure_ascii=False, indent=2))
    if result.report['status'] != 'ready':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
