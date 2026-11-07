import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parser.contract import BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA
from parser.validation import validate_output, validate_rows

ARXIV = '2410.23025'


def _rows():
    return {
        'elements': [
            dict(element_id='2410-23025#root1', arxiv_id=ARXIV,
                 kind='document', content='Document'),
            dict(element_id='2410-23025#s1', arxiv_id=ARXIV,
                 kind='sentence', content=r'See \cite{key}.'),
        ],
        'hierarchy': [
            dict(arxiv_id=ARXIV, version=1,
                 element_id='2410-23025#s1', parent_id='2410-23025#root1', position=0),
        ],
        'bibtex': [
            dict(arxiv_id=ARXIV, citation_key='key', entry_type='misc',
                 title='Title', authors=['Author'], year=2024, doi=None,
                 journal=None, url=None),
        ],
    }


def test_validate_rows_accepts_consistent_publication():
    result = validate_rows(ARXIV, _rows())
    assert result['ok'] is True
    assert result['errors'] == []
    assert result['warnings'] == []


def test_validate_rows_rejects_duplicate_element_ids():
    rows = _rows()
    rows['elements'].append(dict(rows['elements'][0]))
    result = validate_rows(ARXIV, rows)
    assert result['ok'] is False
    assert any(error['code'] == 'duplicate_element_id' for error in result['errors'])


def test_validate_rows_rejects_foreign_element_prefix():
    rows = _rows()
    rows['elements'][1]['element_id'] = 'other-23025#s1'
    rows['hierarchy'][0]['element_id'] = 'other-23025#s1'
    result = validate_rows(ARXIV, rows)
    assert any(error['code'] == 'element_id_prefix_mismatch'
               for error in result['errors'])


def test_validate_rows_rejects_missing_parent_and_bad_version():
    rows = _rows()
    rows['hierarchy'][0]['parent_id'] = '2410-23025#missing'
    rows['hierarchy'][0]['version'] = 0
    result = validate_rows(ARXIV, rows)
    codes = {error['code'] for error in result['errors']}
    assert {'edge_missing_parent', 'invalid_version'} <= codes


def test_validate_rows_reports_unresolved_citation_as_warning():
    rows = _rows()
    rows['elements'][1]['content'] = r'See \cite{missing}.'
    result = validate_rows(ARXIV, rows)
    assert result['ok'] is True
    assert any(warning['code'] == 'unresolved_citation' for warning in result['warnings'])


def test_validate_output_checks_all_tables_and_schemas(tmp_path):
    missing = validate_output(tmp_path)
    assert missing['ok'] is False
    assert {error['code'] for error in missing['errors']} == {'missing_output_table'}

    for name, schema in (('elements', ELEMENTS_SCHEMA),
                         ('hierarchy', HIERARCHY_SCHEMA),
                         ('bibtex', BIBTEX_SCHEMA)):
        pq.write_table(pa.Table.from_pylist([], schema=schema), tmp_path / f'{name}.parquet')
    assert validate_output(tmp_path)['ok'] is True

    pq.write_table(pa.table({'wrong': pa.array([], type=pa.string())}),
                   tmp_path / 'elements.parquet')
    result = validate_output(tmp_path)
    assert result['ok'] is False
    assert any(error['code'] == 'schema_mismatch' for error in result['errors'])


@pytest.mark.parametrize('position', [None, -1, True, '0'])
def test_validate_rows_rejects_invalid_sibling_position(position):
    rows = _rows()
    rows['hierarchy'][0]['position'] = position
    result = validate_rows(ARXIV, rows)
    assert any(e['code'] == 'invalid_sibling_position' for e in result['errors'])


def test_validate_rows_rejects_duplicate_and_gapped_positions():
    rows = _rows()
    rows['elements'].append(dict(rows['elements'][1], element_id='2410-23025#s2'))
    rows['hierarchy'].append(dict(rows['hierarchy'][0], element_id='2410-23025#s2'))
    assert any(e['code'] == 'duplicate_sibling_position'
               for e in validate_rows(ARXIV, rows)['errors'])
    rows['hierarchy'][1]['position'] = 2
    assert any(e['code'] == 'noncontiguous_sibling_positions'
               for e in validate_rows(ARXIV, rows)['errors'])
