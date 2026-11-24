"""Publication-scoped candidate pairs, without assigning training labels."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


BIB_FIELDS = [
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('entry_type', pa.string()),
    pa.field('title', pa.string()),
    pa.field('authors', pa.list_(pa.string())),
    pa.field('year', pa.int64()),
    pa.field('doi', pa.string()),
    pa.field('journal', pa.string()),
    pa.field('url', pa.string()),
]
REF_FIELDS = [
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('ref_arxiv_id', pa.string(), nullable=False),
    pa.field('ref_title', pa.string()),
    pa.field('ref_authors', pa.list_(pa.string())),
    pa.field('ref_submission_date', pa.string()),
    pa.field('ref_s2_id', pa.string()),
    pa.field('ref_doi', pa.string()),
]
PAIR_SCHEMA = pa.schema([
    BIB_FIELDS[0], BIB_FIELDS[1], REF_FIELDS[1],
    *(pa.field('bib_' + field.name, field.type) for field in BIB_FIELDS[2:]),
    *REF_FIELDS[2:],
])
SUMMARY_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('bibtex_entries', pa.int64(), nullable=False),
    pa.field('candidate_references', pa.int64(), nullable=False),
    pa.field('candidate_pairs', pa.int64(), nullable=False),
    pa.field('manual_eligible', pa.bool_(), nullable=False),
])


def _read_parquet(source):
    """Read a table file, shard directory, or explicit list of shard files."""
    sources = [source] if isinstance(source, (str, Path)) else list(source)
    files = []
    for item in sources:
        path = Path(item)
        files.extend(sorted(path.rglob('*.parquet')) if path.is_dir() else [path])
    files = sorted(set(files))
    if not files:
        raise ValueError('No Parquet files found in the selected table')
    tables = [pq.ParquetFile(path).read() for path in files]
    return pa.concat_tables(tables, promote_options='permissive')


def _records(table, fields, keys, *, exclude_missing_reference=False):
    missing = set(keys) - set(table.column_names)
    if missing:
        raise ValueError(f'Missing required columns: {sorted(missing)}')
    rows = table.select([field.name for field in fields if field.name in table.column_names]).to_pylist()
    unique = {}
    excluded = duplicates = 0
    for row in rows:
        publication = row['arxiv_id']
        if not isinstance(publication, str) or not publication.strip():
            raise ValueError('arxiv_id must be a non-empty string')
        if exclude_missing_reference and row['ref_arxiv_id'] in (None, ''):
            excluded += 1
            continue
        for key in keys:
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f'{key} must be a non-empty string')
            if row[key] != row[key].strip():
                raise ValueError(f'{key} has surrounding whitespace')
        row = {field.name: row.get(field.name) for field in fields}
        key = tuple(row[name] for name in keys)
        if key in unique:
            if unique[key] != row:
                raise ValueError(f'Conflicting rows for identity {key!r}')
            duplicates += 1
        else:
            unique[key] = row
    ordered = [unique[key] for key in sorted(unique)]
    # Validate field types without converting missing metadata to empty strings.
    return pa.Table.from_pylist(ordered, schema=pa.schema(fields)), excluded, duplicates


@dataclass(frozen=True)
class CandidatePool:
    entries: pa.Table
    references: pa.Table
    summary: pa.Table
    excluded_reference_rows: int
    duplicate_entry_rows: int
    duplicate_reference_rows: int

    @property
    def pair_count(self):
        return sum(self.summary.column('candidate_pairs').to_pylist())

    def iter_pair_batches(self, batch_size=65536):
        """Emit the full m*n space per publication in bounded Arrow batches."""
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError('batch_size must be a positive integer')
        references = defaultdict(list)
        for reference in self.references.to_pylist():
            references[reference['arxiv_id']].append(reference)
        batch = []
        for entry in self.entries.to_pylist():
            for reference in references[entry['arxiv_id']]:
                pair = {
                    'arxiv_id': entry['arxiv_id'],
                    'citation_key': entry['citation_key'],
                    'ref_arxiv_id': reference['ref_arxiv_id'],
                    **{'bib_' + field.name: entry[field.name] for field in BIB_FIELDS[2:]},
                    **{field.name: reference[field.name] for field in REF_FIELDS[2:]},
                }
                batch.append(pair)
                if len(batch) == batch_size:
                    yield pa.RecordBatch.from_pylist(batch, schema=PAIR_SCHEMA)
                    batch = []
        if batch:
            yield pa.RecordBatch.from_pylist(batch, schema=PAIR_SCHEMA)


def build_candidate_pool(bibtex, references):
    """Accept Arrow tables or Parquet paths and keep entries with zero candidates."""
    if not isinstance(bibtex, pa.Table):
        bibtex = _read_parquet(bibtex)
    if not isinstance(references, pa.Table):
        references = _read_parquet(references)
    entries, _, duplicate_entries = _records(
        bibtex, BIB_FIELDS, ('arxiv_id', 'citation_key'))
    refs, excluded, duplicate_refs = _records(
        references, REF_FIELDS, ('arxiv_id', 'ref_arxiv_id'),
        exclude_missing_reference=True)
    entry_counts = defaultdict(int)
    ref_counts = defaultdict(int)
    for publication in entries.column('arxiv_id').to_pylist():
        entry_counts[publication] += 1
    for publication in refs.column('arxiv_id').to_pylist():
        ref_counts[publication] += 1
    summaries = [
        dict(arxiv_id=publication, bibtex_entries=count,
             candidate_references=ref_counts[publication],
             candidate_pairs=count * ref_counts[publication], manual_eligible=count >= 20)
        for publication, count in sorted(entry_counts.items())
    ]
    return CandidatePool(entries, refs, pa.Table.from_pylist(summaries, schema=SUMMARY_SCHEMA),
                         excluded, duplicate_entries, duplicate_refs)


def write_candidate_pool(pool, output, *, batch_size=65536):
    """Write a new output directory; failed exports remain visibly incomplete."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    pq.write_table(pool.entries, output / 'entries.parquet')
    pq.write_table(pool.references, output / 'references.parquet')
    pq.write_table(pool.summary, output / 'summary.parquet')
    with pq.ParquetWriter(output / 'candidate_pairs.parquet', PAIR_SCHEMA) as writer:
        for batch in pool.iter_pair_batches(batch_size):
            writer.write_batch(batch)
    (output / '_SUCCESS').touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bibtex', required=True, type=Path)
    parser.add_argument('--references', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--batch-size', default=65536, type=int)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error('--batch-size must be positive')
    pool = build_candidate_pool(args.bibtex, args.references)
    write_candidate_pool(pool, args.out, batch_size=args.batch_size)
    print(f'Entries: {pool.entries.num_rows}; candidate pairs: {pool.pair_count}')
    print(f'Excluded references without arXiv ID: {pool.excluded_reference_rows}')
    print(f'Identical duplicate rows: bibtex={pool.duplicate_entry_rows}, '
          f'references={pool.duplicate_reference_rows}')
    print(f'Output: {args.out}')


if __name__ == '__main__':
    main()
