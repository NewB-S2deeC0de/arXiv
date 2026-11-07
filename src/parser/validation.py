"""Structural validation for the three C3 output tables."""
from __future__ import annotations

import re
from pathlib import Path

import pyarrow.parquet as pq

from .contract import (BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA,
                       ElementKind, publication_key)

CITE_RE = re.compile(
    r'\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|nocite)'
    r'\*?(?:\[[^\]]*\])*\{([^}]*)\}'
)
TABLES = {
    'elements': ELEMENTS_SCHEMA,
    'hierarchy': HIERARCHY_SCHEMA,
    'bibtex': BIBTEX_SCHEMA,
}
KINDS = {str(kind) for kind in ElementKind}


class ValidationError(ValueError):
    """Raised when a publication would produce structurally invalid output."""


def cited_keys(content: str) -> set[str]:
    keys: set[str] = set()
    for match in CITE_RE.finditer(content or ''):
        for key in match.group(1).split(','):
            stripped = key.strip()
            if stripped:
                keys.add(stripped)
    return keys


def _error(errors: list[dict], code: str, **context) -> None:
    errors.append(dict(code=code, **context))


def _warning(warnings: list[dict], code: str, **context) -> None:
    warnings.append(dict(code=code, **context))


def validate_rows(arxiv_id: str, rows: dict[str, list[dict]]) -> dict:
    """Validate one publication before its shards are written."""
    errors: list[dict] = []
    warnings: list[dict] = []
    elements = rows.get('elements', [])
    hierarchy = rows.get('hierarchy', [])
    bibtex = rows.get('bibtex', [])

    elements_by_id: dict[str, dict] = {}
    id_prefix = publication_key(arxiv_id) + '#'
    for index, row in enumerate(elements):
        element_id = row.get('element_id')
        if not element_id:
            _error(errors, 'missing_element_id', row=index)
            continue
        if element_id in elements_by_id:
            _error(errors, 'duplicate_element_id', element_id=element_id)
        else:
            elements_by_id[element_id] = row
        if not element_id.startswith(id_prefix):
            _error(errors, 'element_id_prefix_mismatch', element_id=element_id,
                   expected_prefix=id_prefix)
        if row.get('arxiv_id') != arxiv_id:
            _error(errors, 'element_publication_mismatch', element_id=element_id,
                   arxiv_id=row.get('arxiv_id'))
        if row.get('kind') not in KINDS:
            _error(errors, 'invalid_element_kind', element_id=element_id, kind=row.get('kind'))
        content = row.get('content')
        if content is None:
            _warning(warnings, 'null_element_content', element_id=element_id)
        elif not isinstance(content, str):
            _error(errors, 'invalid_element_content_type', element_id=element_id,
                   content_type=type(content).__name__)

    edge_keys: set[tuple] = set()
    sibling_positions: dict[tuple, list[int]] = {}
    for index, row in enumerate(hierarchy):
        element_id = row.get('element_id')
        parent_id = row.get('parent_id')
        version = row.get('version')
        position = row.get('position')
        if type(position) is not int or position < 0:
            _error(errors, 'invalid_sibling_position', row=index, position=position)
        else:
            sibling_positions.setdefault((version, parent_id), []).append(position)
        if row.get('arxiv_id') != arxiv_id:
            _error(errors, 'edge_publication_mismatch', row=index,
                   arxiv_id=row.get('arxiv_id'))
        if not isinstance(version, int) or version < 1:
            _error(errors, 'invalid_version', row=index, version=version)
        if not element_id or element_id not in elements_by_id:
            _error(errors, 'edge_missing_element', row=index, element_id=element_id)
        if not parent_id or parent_id not in elements_by_id:
            _error(errors, 'edge_missing_parent', row=index, parent_id=parent_id)
        if element_id and element_id == parent_id:
            _error(errors, 'edge_self_parent', element_id=element_id)
        key = (version, element_id, parent_id)
        if key in edge_keys:
            _error(errors, 'duplicate_edge', version=version,
                   element_id=element_id, parent_id=parent_id)
        edge_keys.add(key)

    for (version, parent_id), positions in sibling_positions.items():
        if len(set(positions)) != len(positions):
            _error(errors, 'duplicate_sibling_position', version=version, parent_id=parent_id)
        if sorted(positions) != list(range(len(positions))):
            _error(errors, 'noncontiguous_sibling_positions', version=version, parent_id=parent_id)

    bibtex_keys: set[str] = set()
    for index, row in enumerate(bibtex):
        key = row.get('citation_key')
        if not key:
            _error(errors, 'missing_citation_key', row=index)
            continue
        if key in bibtex_keys:
            _error(errors, 'duplicate_citation_key', citation_key=key)
        bibtex_keys.add(key)
        if row.get('arxiv_id') != arxiv_id:
            _error(errors, 'bibtex_publication_mismatch', row=index,
                   arxiv_id=row.get('arxiv_id'))

    for element_id, row in elements_by_id.items():
        for key in sorted(cited_keys(row.get('content') or '')):
            if key not in bibtex_keys:
                _warning(warnings, 'unresolved_citation', element_id=element_id,
                         citation_key=key)

    return dict(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        counts=dict(elements=len(elements), hierarchy=len(hierarchy), bibtex=len(bibtex)),
    )


def validate_or_raise(arxiv_id: str, rows: dict[str, list[dict]]) -> dict:
    result = validate_rows(arxiv_id, rows)
    if not result['ok']:
        first = result['errors'][0]
        raise ValidationError(f'{arxiv_id}: {first["code"]} ({first})')
    return result


def validate_output(output_root: str | Path) -> dict:
    """Check the merged Parquet files expose exactly the C1 schemas."""
    root = Path(output_root)
    errors: list[dict] = []
    for name, schema in TABLES.items():
        path = root / f'{name}.parquet'
        if not path.is_file():
            _error(errors, 'missing_output_table', table=name)
            continue
        actual = pq.read_schema(path)
        if actual != schema:
            _error(errors, 'schema_mismatch', table=name, actual=str(actual), expected=str(schema))
    return dict(ok=not errors, errors=errors, warnings=[])
