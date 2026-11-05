"""Read-only adapter over the Milestone 1 runtime layout (issue C1).

The adapter never writes to ``runtime/data``, ``runtime/progress`` or
``runtime/exports``. It only reads the verified source publications produced by
the crawler and exposes a stable typed contract for the parsing pipeline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

from crawler.storage import digest

from .contract import SourceRecord, SourceStatus

NEW_STYLE_ID = re.compile(r'^\d{4}\.\d{4,5}$')
OLD_STYLE_ID = re.compile(r'^[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7}$')
VERSION_DIR = re.compile(r'^v(\d+)$')


def is_arxiv_id(value: str) -> bool:
    return bool(NEW_STYLE_ID.match(value) or OLD_STYLE_ID.match(value))


class AdapterError(RuntimeError):
    pass


def data_root(persistent_root: str | Path) -> Path:
    return Path(persistent_root) / 'data'


def list_publications(persistent_root: str | Path) -> list[tuple[str, Path]]:
    """Return ``(arxiv_id, paper_dir)`` for every crawled publication, sorted."""
    root = data_root(persistent_root)
    if not root.exists():
        return []
    publications = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        arxiv_id = unquote(directory.name)
        if is_arxiv_id(arxiv_id):
            publications.append((arxiv_id, directory))
    return publications


def version_numbers(paper_dir: Path) -> list[int]:
    versions = paper_dir / 'versions'
    if not versions.exists():
        return []
    found = []
    for directory in versions.iterdir():
        match = VERSION_DIR.match(directory.name)
        if match and directory.is_dir():
            found.append(int(match.group(1)))
    return sorted(found)


def _read_marker(source_dir: Path) -> dict:
    marker_path = source_dir / 'complete.json'
    if not marker_path.is_file():
        raise AdapterError('missing_complete_marker')
    try:
        marker = json.loads(marker_path.read_text(encoding='utf8'))
    except (OSError, ValueError) as exc:
        raise AdapterError('invalid_complete_marker') from exc
    if not isinstance(marker, dict) or not isinstance(marker.get('checksums'), dict):
        raise AdapterError('invalid_complete_marker')
    return marker


def _verify_inventory(source_dir: Path, checksums: dict) -> None:
    """Verify the manifest entry the parser depends on, without rewriting files."""
    expected = checksums.get('inventory.json')
    if not expected:
        raise AdapterError('missing_inventory_checksum')
    inventory = source_dir / 'inventory.json'
    if not inventory.is_file():
        raise AdapterError('missing_inventory')
    if digest(inventory) != expected:
        raise AdapterError('inventory_checksum_mismatch')


def read_source_record(arxiv_id: str, version: int, version_dir: Path) -> SourceRecord:
    source_dir = version_dir / 'source'
    if not source_dir.is_dir():
        return SourceRecord(arxiv_id, version, SourceStatus.MISSING,
                            error='missing_source_directory')
    try:
        marker = _read_marker(source_dir)
    except AdapterError as exc:
        return SourceRecord(arxiv_id, version, SourceStatus.FAILED, source_dir,
                            error=str(exc))

    if marker.get('key') != [arxiv_id, version, 'source', 0]:
        return SourceRecord(arxiv_id, version, SourceStatus.FAILED, source_dir,
                            error='marker_key_mismatch')
    status = marker.get('status')
    if status not in ('succeeded', 'no_tex'):
        return SourceRecord(arxiv_id, version, SourceStatus.FAILED, source_dir,
                            error=f'unexpected_marker_status:{status}')
    try:
        _verify_inventory(source_dir, marker['checksums'])
    except AdapterError as exc:
        return SourceRecord(arxiv_id, version, SourceStatus.FAILED, source_dir,
                            error=str(exc))

    tex_dir = source_dir / 'tex'
    tex_files = tuple(sorted(tex_dir.rglob('*.tex'))) if tex_dir.is_dir() else ()
    bib_files = tuple(sorted(tex_dir.rglob('*.bib'))) if tex_dir.is_dir() else ()
    resolved = SourceStatus.SUCCEEDED if status == 'succeeded' else SourceStatus.NO_TEX
    return SourceRecord(arxiv_id, version, resolved, source_dir,
                        tex_files=tex_files, bib_files=bib_files)


def iter_source_records(persistent_root: str | Path, *, limit: int | None = None):
    """Yield one :class:`SourceRecord` per crawled version, deterministically.

    ``limit`` counts version records here; the C3 CLI counts publications and uses
    :func:`publications_with_records` instead.
    """
    count = 0
    for arxiv_id, paper_dir in list_publications(persistent_root):
        for version in version_numbers(paper_dir):
            yield read_source_record(arxiv_id, version, paper_dir / 'versions' / f'v{version}')
            count += 1
            if limit is not None and count >= limit:
                return


def publications_with_records(persistent_root: str | Path, *, limit: int | None = None
                              ) -> list[tuple[str, list[SourceRecord]]]:
    """Group records per publication; ``limit`` counts publications, not versions."""
    result: list[tuple[str, list[SourceRecord]]] = []
    publications = list_publications(persistent_root)
    if limit is not None:
        publications = publications[:limit]
    for arxiv_id, paper_dir in publications:
        records = [read_source_record(arxiv_id, version, paper_dir / 'versions' / f'v{version}')
                   for version in version_numbers(paper_dir)]
        result.append((arxiv_id, records))
    return result
