import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool, write_candidate_pool


def test_cartesian_space_is_publication_scoped_and_preserves_zero_candidate_queries(tmp_path):
    entries = pa.Table.from_pylist([
        {'arxiv_id': 'p', 'citation_key': 'b', 'title': 'Second'},
        {'arxiv_id': 'p', 'citation_key': 'a', 'title': 'First'},
        {'arxiv_id': 'q', 'citation_key': 'a', 'title': None},
        {'arxiv_id': 'r', 'citation_key': 'a', 'title': 'No candidates'},
    ])
    references = pa.Table.from_pylist([
        {'arxiv_id': 'p', 'ref_arxiv_id': 'ref2'},
        {'arxiv_id': 'p', 'ref_arxiv_id': 'ref1'},
        {'arxiv_id': 'p', 'ref_arxiv_id': 'ref1'},
        {'arxiv_id': 'q', 'ref_arxiv_id': 'ref1'},
        {'arxiv_id': 'r', 'ref_arxiv_id': None},
    ])
    pool = build_candidate_pool(entries, references)
    batches = list(pool.iter_pair_batches(batch_size=2))
    pairs = pa.Table.from_batches(batches).to_pylist()
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert {(r['arxiv_id'], r['citation_key'], r['ref_arxiv_id']) for r in pairs} == {
        ('p', 'a', 'ref1'), ('p', 'a', 'ref2'),
        ('p', 'b', 'ref1'), ('p', 'b', 'ref2'), ('q', 'a', 'ref1'),
    }
    assert pool.pair_count == 5
    assert pool.entries.num_rows == 4
    assert pool.summary.to_pylist()[-1]['candidate_pairs'] == 0
    assert pool.excluded_reference_rows == pool.duplicate_reference_rows == 1
    assert all(row['bib_doi'] is None for row in pairs)
    assert 'label' not in pairs[0]
    out = tmp_path / 'pool'
    write_candidate_pool(pool, out, batch_size=2)
    assert (out / '_SUCCESS').is_file()
    assert pq.read_table(out / 'candidate_pairs.parquet').to_pylist() == pairs
    assert pq.read_table(out / 'entries.parquet').num_rows == 4
    with pytest.raises(FileExistsError):
        write_candidate_pool(pool, out)


@pytest.mark.parametrize('kind,rows', [
    ('bibtex', [
        {'arxiv_id': 'p', 'citation_key': 'a', 'title': 'One'},
        {'arxiv_id': 'p', 'citation_key': 'a', 'title': 'Two'},
    ]),
    ('references', [
        {'arxiv_id': 'p', 'ref_arxiv_id': 'r', 'ref_title': 'One'},
        {'arxiv_id': 'p', 'ref_arxiv_id': 'r', 'ref_title': 'Two'},
    ]),
])
def test_conflicting_identity_rows_are_rejected(kind, rows):
    bibtex = pa.Table.from_pylist([{'arxiv_id': 'p', 'citation_key': 'a'}])
    references = pa.Table.from_pylist([{'arxiv_id': 'p', 'ref_arxiv_id': 'r'}])
    if kind == 'bibtex':
        bibtex = pa.Table.from_pylist(rows)
    else:
        references = pa.Table.from_pylist(rows)
    with pytest.raises(ValueError, match='Conflicting rows'):
        build_candidate_pool(bibtex, references)


def test_reads_multiple_shards_and_writes_empty_pair_table(tmp_path):
    shard_dir = tmp_path / 'bibtex'
    shard_dir.mkdir()
    for index, key in enumerate(('a', 'b')):
        pq.write_table(pa.Table.from_pylist([{'arxiv_id': 'p', 'citation_key': key}]),
                       shard_dir / f'{index}.parquet')
    refs = tmp_path / 'references.parquet'
    pq.write_table(pa.table({'arxiv_id': pa.array([], type=pa.string()),
                            'ref_arxiv_id': pa.array([], type=pa.string())}), refs)
    pool = build_candidate_pool(shard_dir, refs)
    assert pool.entries.num_rows == 2
    assert pool.pair_count == 0
    out = tmp_path / 'pool'
    write_candidate_pool(pool, out)
    assert pq.read_table(out / 'candidate_pairs.parquet').num_rows == 0
    assert pq.read_table(out / 'entries.parquet').num_rows == 2


def test_missing_columns_and_invalid_identifiers_are_rejected():
    refs = pa.Table.from_pylist([{'arxiv_id': 'p', 'ref_arxiv_id': 'r'}])
    with pytest.raises(ValueError, match='Missing required columns'):
        build_candidate_pool(pa.table({'arxiv_id': ['p']}), refs)
    with pytest.raises(ValueError, match='non-empty'):
        build_candidate_pool(pa.table({'arxiv_id': ['p'], 'citation_key': [None]}), refs)


def test_manual_eligibility_counts_unique_entries():
    rows = [{'arxiv_id': 'p', 'citation_key': f'k{index}'} for index in range(20)]
    rows.append(rows[0])
    refs = pa.Table.from_pylist([{'arxiv_id': 'p', 'ref_arxiv_id': 'r'}])
    pool = build_candidate_pool(pa.Table.from_pylist(rows), refs)
    assert pool.summary.to_pylist()[0]['manual_eligible'] is True
    assert pool.entries.num_rows == 20
    assert pool.duplicate_entry_rows == 1
