"""Kiểm tra output M2 khôi phục theo batch, dùng SQLite tạm trên đĩa."""

import argparse
from collections import Counter
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq


KEY_COLUMNS = {
    'elements': ['arxiv_id', 'element_id'],
    'bibtex': ['arxiv_id', 'citation_key'],
    'hierarchy': ['arxiv_id', 'version', 'element_id', 'parent_id'],
}


def validate_recovery(root, *, expected_publications=None, batch_size=2048,
                      workdir=None, log=print):
    """Đối chiếu bảng với checkpoint/coverage, không tái tạo audit gốc."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('batch_size phải nguyên dương')
    if expected_publications is not None and (
            type(expected_publications) is not int or expected_publications < 1):
        raise ValueError('expected_publications phải nguyên dương')
    root = Path(root)
    errors, warnings, counts = [], [], {}
    with tempfile.TemporaryDirectory(prefix='m2-check-', dir=workdir) as temporary:
        with closing(sqlite3.connect(Path(temporary) / 'keys.sqlite')) as db:
            db.execute('PRAGMA cache_size=-32768')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('CREATE TABLE elements(pub TEXT, id TEXT, PRIMARY KEY(pub,id))')
            db.execute('CREATE TABLE bibtex(pub TEXT, id TEXT, PRIMARY KEY(pub,id))')
            db.execute('CREATE TABLE hierarchy(pub TEXT, version INTEGER, id TEXT, parent TEXT)')
            for name, keys in KEY_COLUMNS.items():
                log(f'Đang đọc từng batch — {name}', flush=True)
                path = root / f'{name}.parquet'
                try:
                    parquet = pq.ParquetFile(path)
                    if set(keys) - set(parquet.schema_arrow.names):
                        raise ValueError(f'Thiếu cột khóa {keys}')
                    if name == 'elements' and not {'kind', 'content'} <= set(parquet.schema_arrow.names):
                        raise ValueError('Thiếu kind/content')
                    if name == 'hierarchy' and 'position' not in parquet.schema_arrow.names:
                        warnings.append(dict(code='hierarchy_without_position'))
                    total = duplicates = invalid = 0
                    for batch in parquet.iter_batches(batch_size=batch_size):
                        # Decode mọi cột để phát hiện lỗi đọc bên trong từng batch.
                        rows = batch.to_pylist()
                        valid = []
                        for row in rows:
                            identity = [row[key] for key in keys]
                            good = all(
                                type(value) is int and value > 0 if key == 'version'
                                else isinstance(value, str) and bool(value.strip())
                                for key, value in zip(keys, identity)
                            )
                            if good:
                                valid.append(identity)
                            else:
                                invalid += 1
                        before = db.total_changes
                        placeholders = ','.join('?' for _ in keys)
                        db.executemany(f'INSERT OR IGNORE INTO {name} VALUES ({placeholders})', valid)
                        duplicates += len(valid) - (db.total_changes - before)
                        total += len(rows)
                        db.commit()
                    counts[name] = total
                    if duplicates:
                        errors.append(dict(code='duplicate_key', table=name, count=duplicates))
                    if invalid:
                        errors.append(dict(code='invalid_key', table=name, count=invalid))
                    log(f'Đã đọc hết — {name}: {total} dòng', flush=True)
                except (OSError, ValueError, pa.ArrowException) as exc:
                    errors.append(dict(code='table_read_failed', table=name, message=str(exc)))
            if set(counts) == set(KEY_COLUMNS):
                db.execute('CREATE INDEX hierarchy_edges ON hierarchy(pub,version,id,parent)')
                duplicate_edges = db.execute(
                    'SELECT COALESCE(SUM(n-1),0) FROM '
                    '(SELECT COUNT(*) n FROM hierarchy GROUP BY pub,version,id,parent HAVING COUNT(*)>1)'
                ).fetchone()[0]
                if duplicate_edges:
                    errors.append(dict(code='duplicate_hierarchy_edge', count=duplicate_edges))
                for column in ('id', 'parent'):
                    missing = db.execute(
                        f'SELECT COUNT(*) FROM hierarchy h LEFT JOIN elements e '
                        f'ON e.pub=h.pub AND e.id=h.{column} WHERE e.id IS NULL'
                    ).fetchone()[0]
                    if missing:
                        errors.append(dict(code=f'hierarchy_missing_{column}', count=missing))
                self_edges = db.execute('SELECT COUNT(*) FROM hierarchy WHERE id=parent').fetchone()[0]
                if self_edges:
                    errors.append(dict(code='hierarchy_self_parent', count=self_edges))
            actual = {}
            for name in ('elements', 'bibtex'):
                actual[name] = dict(db.execute(f'SELECT pub,COUNT(*) FROM {name} GROUP BY pub'))
            try:
                with (root / 'coverage.csv').open(encoding='utf-8-sig', newline='') as handle:
                    coverage = {}
                    for row in csv.DictReader(handle):
                        publication = row['arxiv_id']
                        if publication in coverage:
                            raise ValueError('Coverage có publication bị lặp')
                        coverage[publication] = row
                checkpoint_path = root / 'checkpoint.json'
                if checkpoint_path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError('Checkpoint vượt 64 MiB; cần đọc streaming riêng')
                checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
                processed = checkpoint['processed']
                if set(coverage) != set(processed):
                    errors.append(dict(code='coverage_checkpoint_publication_mismatch'))
                if expected_publications is not None and len(processed) != expected_publications:
                    errors.append(dict(code='publication_count_mismatch',
                        actual=len(processed), expected=expected_publications))
                for name, field in (('elements', 'elements_after'), ('bibtex', 'bibtex_after')):
                    for publication in sorted(set(processed) | set(actual[name]) | set(coverage)):
                        expected = processed.get(publication, {}).get('stats', {}).get(field)
                        coverage_count = coverage.get(publication, {}).get(field)
                        observed = actual[name].get(publication, 0)
                        if (type(expected) is not int or coverage_count is None
                                or expected != int(coverage_count) or observed != expected):
                            errors.append(dict(code='publication_rows_mismatch', table=name,
                                arxiv_id=publication, parquet=observed,
                                checkpoint=expected, coverage=coverage_count))
                version_counts = Counter()
                for item in processed.values():
                    for key, value in item.get('stats', {}).items():
                        if key.startswith('versions_') and type(value) is int:
                            version_counts[key] += value
                publication_count = len(processed)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(dict(code='progress_comparison_failed', message=str(exc)))
                publication_count, version_counts = None, {}
    return dict(
        status='checks_passed' if not errors else 'failed', rows=counts,
        checkpoint_publications=publication_count, versions=dict(version_counts),
        errors=errors, warnings=warnings,
        limitations=[
            'Kiểm tra độ đọc được, khóa, foreign key và đối chiếu số dòng với progress.',
            'Không thay manifest/audit gốc, không xác nhận chất lượng nội dung hay toàn bộ cấu trúc cây.',
            'Số publication được kiểm tra theo số lượng mong đợi; chưa đối chiếu danh sách ID nguồn M1.',
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--expected-publications', type=int)
    parser.add_argument('--batch-size', type=int, default=2048)
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    report = validate_recovery(args.input, expected_publications=args.expected_publications,
                               batch_size=args.batch_size, workdir=args.workdir)
    with args.report.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != 'errors'},
                     ensure_ascii=False, indent=2))
    print('Số vấn đề:', len(report['errors']))
    if report['status'] != 'checks_passed':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
