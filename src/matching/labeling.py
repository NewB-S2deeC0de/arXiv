"""Interactive manual labeling with atomic, snapshot-bound progress."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from .candidate_pool import build_candidate_pool


LABEL_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('groundtruth_arxiv_id', pa.string()),
    pa.field('label_status', pa.string(), nullable=False),
    pa.field('reviewer', pa.string(), nullable=False),
    pa.field('updated_at', pa.string(), nullable=False),
])


def pool_fingerprint(pool):
    payload = {'entries': pool.entries.to_pylist(), 'references': pool.references.to_pylist()}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


@contextmanager
def _exclusive_progress(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f'Labeling progress is already locked: {lock}') from None
    os.close(descriptor)
    try:
        yield
    finally:
        lock.unlink()


def _load_labels(path, fingerprint, entries, references):
    if not path.exists():
        return {}
    table = pq.ParquetFile(path).read()
    metadata = table.schema.metadata or {}
    if metadata.get(b'candidate_pool_sha256') != fingerprint.encode('ascii'):
        raise ValueError('Candidate snapshot changed; use a new progress path or review labels explicitly')
    if not table.schema.remove_metadata().equals(LABEL_SCHEMA):
        raise ValueError('Unexpected manual label schema')
    labels = {}
    for row in table.to_pylist():
        key = (row['arxiv_id'], row['citation_key'])
        target = row['groundtruth_arxiv_id']
        if key not in entries or key in labels:
            raise ValueError(f'Invalid or duplicate label identity: {key}')
        valid_match = row['label_status'] == 'matched' and target in references.get(key[0], set())
        valid_none = row['label_status'] == 'no_match' and target is None
        if not (valid_match or valid_none) or not row['reviewer']:
            raise ValueError(f'Invalid label decision: {key}')
        labels[key] = row
    return labels


def _save_labels(path, labels, fingerprint):
    table = pa.Table.from_pylist([labels[key] for key in sorted(labels)], schema=LABEL_SCHEMA)
    table = table.replace_schema_metadata({b'candidate_pool_sha256': fingerprint.encode('ascii')})
    descriptor, filename = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    os.close(descriptor)
    temporary = Path(filename)
    try:
        pq.write_table(table, temporary)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _show_record(row, *, reference, output):
    prefix = 'ref_' if reference else ''
    authors = row.get(prefix + 'authors')
    date = row.get('ref_submission_date') if reference else None
    year = (date[:4] if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit()
            else None) if reference else row.get('year')
    output('Tiêu đề — ' + (row.get(prefix + 'title') or '[thiếu dữ liệu]'))
    output('Tác giả — ' + ('; '.join(authors) if authors else '[thiếu dữ liệu]'))
    output('Năm — ' + (str(year) if year is not None else '[thiếu dữ liệu]'))
    output('DOI — ' + (row.get('ref_doi' if reference else 'doi') or '[thiếu dữ liệu]'))
    if reference:
        output('arXiv — https://arxiv.org/abs/' + row['ref_arxiv_id'])
        output('Ngày phía reference — ' + (date or '[thiếu dữ liệu]'))
    else:
        output('Tạp chí — ' + (row.get('journal') or '[thiếu dữ liệu]'))
        output('URL — ' + (row.get('url') or '[thiếu dữ liệu]'))


def run_labeling_session(pool, progress, *, reviewer, publications=None, entry_keys=None,
                         input_fn=input, output=print):
    """Run the same numbered-choice UI in a notebook or terminal.

    Saved matched/no_match rows are decisions. Skipped entries have no label.
    A publication filter limits the UI, while progress retains all publications.
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError('A reviewer name is required')
    all_entries = {(row['arxiv_id'], row['citation_key']): row
                   for row in pool.entries.to_pylist()}
    candidates = {}
    for row in pool.references.to_pylist():
        candidates.setdefault(row['arxiv_id'], []).append(row)
    candidate_ids = {pub: {row['ref_arxiv_id'] for row in rows}
                     for pub, rows in candidates.items()}
    selected = set(publications) if publications is not None else {key[0] for key in all_entries}
    unknown = selected - {key[0] for key in all_entries}
    if unknown:
        raise ValueError(f'Unknown publications: {sorted(unknown)}')
    keys = [key for key in sorted(all_entries) if key[0] in selected]
    if entry_keys is not None:
        requested = {tuple(key) for key in entry_keys}
        if requested - set(keys):
            raise ValueError('Entry selection contains unknown or excluded entries')
        keys = [key for key in keys if key in requested]
    fingerprint = pool_fingerprint(pool)
    path = Path(progress)
    with _exclusive_progress(path):
        labels = _load_labels(path, fingerprint, all_entries, candidate_ids)
        pending = [index for index, key in enumerate(keys) if key not in labels]
        index = pending[0] if pending else 0
        while index < len(keys):
            key = keys[index]
            rows = candidates.get(key[0], [])
            done = sum(item in labels for item in keys)
            output(f'\nEntry {index + 1}/{len(keys)} | đã gán nhãn {done}/{len(keys)}')
            output(f'Bài báo — {key[0]} | Khóa trích dẫn — {key[1]}')
            _show_record(all_entries[key], reference=False, output=output)
            previous = labels.get(key)
            output('Nhãn đã lưu — ' + (
                str(previous['groundtruth_arxiv_id']) if previous and previous['label_status'] == 'matched'
                else 'không có match' if previous else 'chưa gán nhãn'))
            output(f'Ứng viên — {len(rows)}')
            for number, row in enumerate(rows, start=1):
                output(f'\n[{number}] {row["ref_arxiv_id"]}')
                _show_record(row, reference=True, output=output)
            output('\nNhập số để chọn match | n = không có match | s = bỏ qua | p = quay lại | c = xóa nhãn | q = thoát')
            try:
                command = input_fn('Lựa chọn: ').strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if command == 'q':
                break
            if command == 'p':
                index = max(0, index - 1)
                continue
            if command == 's':
                index += 1
                continue
            if command == 'c':
                updated = dict(labels)
                updated.pop(key, None)
                _save_labels(path, updated, fingerprint)
                labels = updated
                continue
            if command == 'n':
                target = None
            elif command.isascii() and command.isdigit() and 1 <= int(command) <= len(rows):
                target = rows[int(command) - 1]['ref_arxiv_id']
            else:
                output('Lựa chọn không hợp lệ. Nhập số ứng viên hoặc một lệnh trong hướng dẫn.')
                continue
            updated = dict(labels)
            updated[key] = {
                'arxiv_id': key[0], 'citation_key': key[1],
                'groundtruth_arxiv_id': target,
                'label_status': 'no_match' if target is None else 'matched',
                'reviewer': reviewer.strip(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            _save_labels(path, updated, fingerprint)
            labels = updated
            output('Đã lưu nhãn.')
            index += 1
        labeled = sum(key in labels for key in keys)
        output(f'Đã kết thúc phiên. Đã gán nhãn {labeled}/{len(keys)} entry được chọn.')
        return pa.Table.from_pylist([labels[key] for key in sorted(labels)], schema=LABEL_SCHEMA)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bibtex', required=True, type=Path)
    parser.add_argument('--references', required=True, type=Path)
    parser.add_argument('--progress', required=True, type=Path)
    parser.add_argument('--reviewer', required=True)
    parser.add_argument('--publication', action='append')
    args = parser.parse_args()
    pool = build_candidate_pool(args.bibtex, args.references)
    run_labeling_session(pool, args.progress, reviewer=args.reviewer,
                         publications=args.publication)


if __name__ == '__main__':
    main()
