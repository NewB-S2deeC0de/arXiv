import csv
import json

import pyarrow as pa
import pyarrow.parquet as pq

from matching.validate_m2_recovery import validate_recovery


def write_fixture(root):
    root.mkdir()
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='p', element_id='root', kind='document', content=''),
        dict(arxiv_id='p', element_id='child', kind='sentence', content='Text'),
    ]), root / 'elements.parquet')
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='p', version=1, element_id='child', parent_id='root'),
    ]), root / 'hierarchy.parquet')
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='p', citation_key='a', title='Reference'),
    ]), root / 'bibtex.parquet')
    with (root / 'coverage.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['arxiv_id', 'elements_after', 'bibtex_after'])
        writer.writeheader()
        writer.writerow(dict(arxiv_id='p', elements_after=2, bibtex_after=1))
    (root / 'checkpoint.json').write_text(json.dumps({
        'processed': {'p': {'stats': {'elements_after': 2, 'bibtex_after': 1,
                                     'versions_total': 1, 'versions_parsed': 1}}},
    }), encoding='utf-8')


def check(root, **kwargs):
    return validate_recovery(root, batch_size=1, log=lambda *args, **kw: None, **kwargs)


def test_recovered_tables_are_checked_against_progress(tmp_path):
    root = tmp_path / 'recovery'
    write_fixture(root)
    report = check(root, expected_publications=1)
    assert report['status'] == 'checks_passed'
    assert report['rows'] == {'elements': 2, 'hierarchy': 1, 'bibtex': 1}
    assert report['versions']['versions_parsed'] == 1
    assert report['warnings'] == [{'code': 'hierarchy_without_position'}]


def test_invalid_foreign_key_and_partial_export_are_detected(tmp_path):
    root = tmp_path / 'recovery'
    write_fixture(root)
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='p', version=1, element_id='child', parent_id='absent'),
    ]), root / 'hierarchy.parquet')
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='p', element_id='root', kind='document', content=''),
    ]), root / 'elements.parquet')
    codes = {item['code'] for item in check(root)['errors']}
    assert {'hierarchy_missing_id', 'hierarchy_missing_parent', 'publication_rows_mismatch'} <= codes


def test_truncated_parquet_is_reported(tmp_path):
    root = tmp_path / 'recovery'
    write_fixture(root)
    path = root / 'bibtex.parquet'
    path.write_bytes(path.read_bytes()[:-10])
    assert any(item['code'] == 'table_read_failed' for item in check(root)['errors'])


def test_duplicate_edges_and_publication_count_are_detected(tmp_path):
    root = tmp_path / 'recovery'
    write_fixture(root)
    table = pq.read_table(root / 'hierarchy.parquet')
    pq.write_table(pa.concat_tables([table, table]), root / 'hierarchy.parquet')
    codes = {item['code'] for item in check(root, expected_publications=2)['errors']}
    assert {'duplicate_hierarchy_edge', 'publication_count_mismatch'} <= codes
