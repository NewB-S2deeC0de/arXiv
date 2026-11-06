"""End-to-end Milestone 2 parsing pipeline (issue C3).

Reads verified Milestone 1 sources read-only, parses every version, deduplicates
across versions and writes three Parquet tables plus a manifest under
``runtime/parsed``. Large datasets are processed publication by publication and
merged by streaming the per-publication shards.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import shutil
import time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq

from crawler.storage import digest

from .adapter import publications_with_records
from .contract import (BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA,
                       GatheredSource, GatherError, ParsedDocument,
                       ParserServices, SourceStatus, TreeNode, VersionInput,
                       bibtex_rows, element_rows, hierarchy_rows)
from .dedup import ElementStore
from .integration import load_services
from .validation import ValidationError, validate_or_raise, validate_output

PARSER_VERSION = '1.0.0'
CHECKPOINT_SCHEMA = 2
MANIFEST_SCHEMA = 1
TABLES = (('elements', ELEMENTS_SCHEMA), ('hierarchy', HIERARCHY_SCHEMA), ('bibtex', BIBTEX_SCHEMA))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _schema_fingerprint() -> str:
    return _sha256_text(str(ELEMENTS_SCHEMA) + str(HIERARCHY_SCHEMA) + str(BIBTEX_SCHEMA))


def _service_fingerprint(services) -> str:
    """Fingerprint a service for policy/checkpoint metadata.

    A service that exposes ``cache_fingerprint()`` is trusted for resume.
    Otherwise fall back to class identity plus a hash of its source file so the
    policy fingerprint still reflects code changes. This fallback cannot see
    runtime configuration, so :func:`run_parser` disables resume for such
    services instead of reusing shards.
    """
    method = getattr(services, 'cache_fingerprint', None)
    if callable(method):
        return str(method())
    cls = type(services)
    source_hash = ''
    try:
        import inspect
        source = inspect.getsourcefile(cls) or inspect.getfile(cls)
    except (TypeError, OSError):
        source = None
    if source:
        try:
            source_hash = _sha256_bytes(Path(source).read_bytes())
        except OSError:
            source_hash = ''
    identity = f'{cls.__module__}.{cls.__qualname__}'
    return _sha256_text(json.dumps([identity, source_hash], sort_keys=True))


def _policy_fingerprint(include_partial: bool,
                        services: ParserServices | None = None) -> str:
    payload = dict(
        parser_version=PARSER_VERSION,
        include_partial=include_partial,
        schema=_schema_fingerprint(),
    )
    if services is not None:
        payload['services'] = _service_fingerprint(services)
    return _sha256_text(json.dumps(payload, sort_keys=True))


def _status_text(status) -> str:
    return status.value if hasattr(status, 'value') else str(status)


def _file_content_fingerprint(path: Path) -> str:
    try:
        with path.open('rb') as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return 'missing'


def _publication_input_fingerprint(arxiv_id: str, records, *,
                                   selected_bib_files=None,
                                   approved_metadata_candidates=None) -> str:
    parts = [arxiv_id]
    for record in sorted(records, key=lambda item: item.version):
        parts.append(f"v{record.version}:{_status_text(record.status)}:{record.error or ''}")
        base = record.source_path
        for path in sorted((record.tex_files or ()) + (record.bib_files or ())):
            try:
                rel = path.relative_to(base) if base is not None else path.name
            except ValueError:
                rel = path.name
            parts.append(f"{rel.as_posix()}:{_file_content_fingerprint(path)}")
    decisions = {}
    if selected_bib_files:
        decisions['selected_bib_files'] = {
            str(version): list(files)
            for (publication, version), files in selected_bib_files.items()
            if publication == arxiv_id
        }
    if approved_metadata_candidates:
        decisions['approved_metadata_candidates'] = list(
            approved_metadata_candidates.get(arxiv_id, ()))
    if decisions:
        parts.append(json.dumps(decisions, sort_keys=True, default=list))
    return _sha256_text('|'.join(parts))


def _spans_of(diagnostic) -> list[dict]:
    spans = getattr(diagnostic, 'source_spans', None)
    if spans:
        return [{'path': s.path, 'start': s.start, 'end': s.end} for s in spans]
    return [{'path': getattr(diagnostic, 'path', None),
               'start': getattr(diagnostic, 'start', None),
               'end': getattr(diagnostic, 'end', None)}]


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        return default


def _guard_output(persistent_root: Path, output_root: Path) -> None:
    root = persistent_root.resolve()
    output = output_root.resolve()
    for name in ('data', 'progress', 'exports'):
        forbidden = (root / name).resolve()
        if output == forbidden or forbidden in output.parents:
            raise ValueError(f'Parsed output must not write into Milestone 1 {name}/')
    if output == root:
        raise ValueError('Parsed output must be a dedicated subdirectory, not the runtime root')


def resolve_output(persistent_root: str | Path, output_root: str | Path | None) -> Path:
    if output_root is None:
        return Path(persistent_root) / 'parsed'
    return Path(output_root)


class SourceTextUnavailable(RuntimeError):
    """Raised when C cannot decode an A1 source file needed for provenance verification."""


def _version_stats() -> dict:
    return dict(versions_total=0, versions_parsed=0, versions_empty=0,
                versions_partial=0, versions_pending=0, versions_failed=0,
                missing=0, gather_failed=0,
                elements_before=0, elements_after=0,
                bibtex_before=0, bibtex_after=0, bibtex_duplicates=0,
                bibtex_unexportable=0, bibtex_unresolved=0,
                citations_unresolved=0, bibtex_conflicts=0,
                bibtex_conflict_fields=0, seconds=0.0)


def _remap_tree(node: TreeNode, mapping: dict[str, str], remap) -> None:
    node.content = remap(node.content, mapping)
    for child in node.children:
        _remap_tree(child, mapping, remap)


def _span_dict(span) -> dict:
    if isinstance(span, dict):
        return dict(path=span.get('path'), start=span.get('start'), end=span.get('end'))
    return dict(path=getattr(span, 'path', None), start=getattr(span, 'start', None),
                end=getattr(span, 'end', None))


def _diagnostic_warning(arxiv_id, version, stage, item, *, path=None) -> dict:
    spans = [_span_dict(span) for span in (getattr(item, 'source_spans', None) or ())]
    start = getattr(item, 'start', None)
    end = getattr(item, 'end', None)
    if not spans and start is not None:
        spans = [dict(path=path, start=start, end=end)]
    first = spans[0] if spans else {}
    return dict(
        arxiv_id=arxiv_id, version=version, status='parse_warning', stage=stage,
        code=str(getattr(item, 'code', 'diagnostic')),
        message=str(getattr(item, 'message', '')),
        severity=str(getattr(item, 'severity', 'warning')),
        path=first.get('path'), start=first.get('start'), end=first.get('end'),
        spans=spans,
    )


def _source_texts(gathered, source_dir: Path) -> dict[str, str]:
    """Decode exactly A1's files; raise if any needed file cannot be read.

    Returning ``None`` silently bypasses provenance verification, so a read
    error is surfaced instead and the caller downgrades the version rather
    than marking it ``ready`` without evidence.
    """
    raw = getattr(gathered, 'raw', None)
    names = getattr(raw, 'used_tex_files', None)
    if not names:
        return {}
    texts = {}
    for name in names:
        try:
            texts[name] = (Path(source_dir) / name).read_text(encoding='utf8')
        except (OSError, UnicodeError) as exc:
            raise SourceTextUnavailable(f'{name}: {exc}') from exc
    return texts


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name))
                for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _standardization_audit(arxiv_id: str, standardized, services,
                           warnings, errors, read_failed=None) -> dict:
    audit = dict(
        schema_version=1,
        arxiv_id=arxiv_id,
        mode='b_standardization',
        versions=[],
        references=None,
        warnings=warnings,
        errors=errors,
    )
    method = getattr(services, 'audit_payload', None)
    if callable(method) and getattr(standardized, 'references', None) is not None:
        audit['references'] = method(standardized)
    for version in getattr(standardized, 'versions', ()) or ():
        finalized = getattr(version, 'finalized', None)
        audit['versions'].append(dict(
            version=getattr(version, 'version', None),
            status=getattr(version, 'status', None),
            normalized=_jsonable(getattr(version, 'normalized', None)),
            bibliography=_jsonable(getattr(version, 'bibliography', None)),
            finalized=None if finalized is None else dict(
                tree=_jsonable(getattr(finalized, 'tree', None)),
                nodes=_jsonable(getattr(finalized, 'nodes', ())),
            ),
        ))
    for item in read_failed or ():
        audit['versions'].append(dict(
            version=item['version'],
            status=item.get('status', 'partial'),
            normalized=None,
            bibliography=None,
            finalized=None,
            reason=item.get('reason', 'source_texts_unavailable'),
            message=item.get('message'),
        ))
    audit['versions'].sort(key=lambda item: str(item.get('version')))
    return _jsonable(audit)


def _legacy_audit(arxiv_id: str, warnings, errors) -> dict:
    return _jsonable(dict(
        schema_version=1, arxiv_id=arxiv_id, mode='legacy_service',
        versions=[], references=None, warnings=warnings, errors=errors,
    ))


def _process_publication_legacy(arxiv_id: str, records, services: ParserServices,
                         include_partial: bool = False
                         ) -> tuple[dict, list[dict], list[dict], dict[str, list[dict]]]:
    stats = _version_stats()
    errors: list[dict] = []
    warnings: list[dict] = []
    store = ElementStore(arxiv_id)
    versioned_bib = []
    gathered_versions = []
    rows: dict[str, list[dict]] = {'elements': [], 'hierarchy': [], 'bibtex': []}

    # First pass: gather sources and unify references, so citation keys are
    # already remapped before element content is deduplicated by the store.
    for record in sorted(records, key=lambda item: item.version):
        stats['versions_total'] += 1
        if record.status is SourceStatus.MISSING:
            stats['missing'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='missing', error=record.error or 'missing'))
            continue
        if record.status is SourceStatus.FAILED:
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=record.error or 'failed'))
            continue
        if record.status is SourceStatus.NO_TEX or not record.tex_files:
            stats['versions_empty'] += 1
            continue
        try:
            gathered = services.gather(arxiv_id, record.version, record.source_path / 'tex')
        except GatherError as exc:
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:{exc}'))
            continue
        except Exception as exc:
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed',
                               error=f'gather:{type(exc).__name__}:{exc}'))
            continue
        for diagnostic in gathered.diagnostics:
            spans = _spans_of(diagnostic)
            first = spans[0] if spans else {}
            warnings.append(dict(
                arxiv_id=arxiv_id, version=record.version, status='gather_warning',
                stage='gather',
                code=diagnostic.code, message=diagnostic.message,
                severity=diagnostic.severity,
                path=first.get('path'), start=first.get('start'), end=first.get('end'),
                spans=spans,
            ))
        if gathered.status == 'unparseable':
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            detail = '; '.join(
                f'{diagnostic.code}: {diagnostic.message}'
                for diagnostic in gathered.diagnostics
            ) or 'unparseable source'
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:unparseable:{detail}'))
            continue
        if gathered.status not in ('ready', 'partial'):
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:invalid_status:{gathered.status}'))
            continue
        gathered_versions.append((record.version, gathered))

    # Parse gathered sources and collect bibliography blocks before unification.
    parsed_versions: list[tuple[int, GatheredSource, ParsedDocument]] = []
    for version, gathered in gathered_versions:
        try:
            parsed = services.parse_document(gathered, arxiv_id, version)
        except Exception as exc:  # parser failures are reported, never silent success
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:{type(exc).__name__}:{exc}'))
            continue
        for diagnostic in parsed.result.diagnostics:
            spans = _spans_of(diagnostic)
            first = spans[0] if spans else {}
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version, status='parse_warning',
                stage='parse',
                code=diagnostic.code, message=diagnostic.message,
                severity=diagnostic.severity,
                path=first.get('path'), start=first.get('start'), end=first.get('end'),
                spans=spans,
            ))
        if parsed.result.status == 'unparseable':
            stats['versions_failed'] += 1
            detail = '; '.join(
                f'{diagnostic.code}: {diagnostic.message}'
                for diagnostic in parsed.result.diagnostics
            ) or 'unparseable source'
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:unparseable:{detail}'))
            continue
        if parsed.result.status not in ('ready', 'partial'):
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:invalid_status:{parsed.result.status}'))
            continue
        parsed_versions.append((version, gathered, parsed))

    # Only eligible versions (ready, or partial with include_partial=True) and a
    # finalized tree are exported to the three tables and used for BibTeX
    # unification. Partial versions skipped by policy keep diagnostics/warnings
    # but produce no rows. A version without finalized tree is counted as
    # pending (A3/B1 not merged yet) and never leaks BibTeX into the output.
    versioned_bib = []
    eligible_parsed: list[tuple[int, GatheredSource, ParsedDocument]] = []
    for version, gathered, parsed in parsed_versions:
        is_partial = gathered.status == 'partial' or parsed.result.status == 'partial'
        if is_partial and not include_partial:
            stats['versions_partial'] += 1
            continue
        if parsed.tree is None:
            stats['versions_pending'] += 1
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version, status='parse_warning',
                stage='parse', code='no_finalized_tree',
                message='Version ready but B1 has not finalized content yet; '
                        'element rows are skipped until B1 merges.',
                path=None, start=None, end=None, spans=[],
            ))
            continue
        eligible_parsed.append((version, gathered, parsed))
        bib_texts = [block.raw for block in parsed.result.bibliography] or [gathered.text]
        for text in bib_texts:
            for entry in services.extract_bibitems(text, arxiv_id):
                versioned_bib.append((version, entry))

    unified, mapping, bib_stats = services.unify_bibentries(versioned_bib)

    # Second pass: remap citations and store only finalized content.
    # Versions with tree=None were already counted as pending above; only
    # finalized trees reach here.
    for version, _, parsed in eligible_parsed:
        _remap_tree(parsed.tree, mapping, services.remap_citations)
        store.add_tree(version, parsed.tree)
        stats['versions_parsed'] += 1

    errors.sort(key=lambda item: (item['version'], item['status']))
    stats['elements_before'] = store.total_nodes
    stats['elements_after'] = store.distinct_elements
    stats['bibtex_before'] = bib_stats['before']
    stats['bibtex_after'] = bib_stats['after']
    stats['bibtex_duplicates'] = bib_stats['duplicates']

    rows['elements'] = element_rows(store.sorted_elements())
    rows['hierarchy'] = hierarchy_rows(store.sorted_edges())
    rows['bibtex'] = bibtex_rows(sorted(unified, key=lambda entry: entry.citation_key))
    validation = validate_or_raise(arxiv_id, rows)
    warnings.extend(validation['warnings'])
    stats['citations_unresolved'] = len({item['citation_key'] for item in validation['warnings']
                                         if item.get('code') == 'unresolved_citation'})
    return stats, errors, warnings, rows


def _process_publication_b(arxiv_id: str, records, services: ParserServices, *,
                           include_partial: bool = False,
                           selected_bib_files=None,
                           approved_metadata_candidates=None):
    stats = _version_stats()
    errors: list[dict] = []
    warnings: list[dict] = []
    store = ElementStore(arxiv_id)
    rows: dict[str, list[dict]] = {'elements': [], 'hierarchy': [], 'bibtex': []}
    gathered_versions = []

    for record in sorted(records, key=lambda item: item.version):
        stats['versions_total'] += 1
        if record.status is SourceStatus.MISSING:
            stats['missing'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='missing', error=record.error or 'missing'))
            continue
        if record.status is SourceStatus.FAILED:
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=record.error or 'failed'))
            continue
        if record.status is SourceStatus.NO_TEX or not record.tex_files:
            stats['versions_empty'] += 1
            continue
        source_dir = record.source_path / 'tex'
        try:
            gathered = services.gather(arxiv_id, record.version, source_dir)
        except GatherError as exc:
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:{exc}'))
            continue
        except Exception as exc:
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed',
                               error=f'gather:{type(exc).__name__}:{exc}'))
            continue
        for diagnostic in gathered.diagnostics:
            spans = _spans_of(diagnostic)
            first = spans[0] if spans else {}
            warnings.append(dict(
                arxiv_id=arxiv_id, version=record.version, status='gather_warning',
                stage='gather',
                code=diagnostic.code, message=diagnostic.message,
                severity=diagnostic.severity,
                path=first.get('path'), start=first.get('start'), end=first.get('end'),
                spans=spans,
            ))
        if gathered.status == 'unparseable':
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            detail = '; '.join(
                f'{diagnostic.code}: {diagnostic.message}'
                for diagnostic in gathered.diagnostics
            ) or 'unparseable source'
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:unparseable:{detail}'))
            continue
        if gathered.status not in ('ready', 'partial'):
            stats['gather_failed'] += 1
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=record.version,
                               status='failed', error=f'gather:invalid_status:{gathered.status}'))
            continue
        gathered_versions.append((record.version, gathered, source_dir))

    parsed_versions = []
    for version, gathered, source_dir in gathered_versions:
        try:
            parsed = services.parse_document(gathered, arxiv_id, version)
        except Exception as exc:
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:{type(exc).__name__}:{exc}'))
            continue
        for diagnostic in parsed.result.diagnostics:
            spans = _spans_of(diagnostic)
            first = spans[0] if spans else {}
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version, status='parse_warning',
                stage='parse',
                code=diagnostic.code, message=diagnostic.message,
                severity=diagnostic.severity,
                path=first.get('path'), start=first.get('start'), end=first.get('end'),
                spans=spans,
            ))
        if parsed.result.status == 'unparseable':
            stats['versions_failed'] += 1
            detail = '; '.join(
                f'{diagnostic.code}: {diagnostic.message}'
                for diagnostic in parsed.result.diagnostics
            ) or 'unparseable source'
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:unparseable:{detail}'))
            continue
        if parsed.result.status not in ('ready', 'partial'):
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed', error=f'parse:invalid_status:{parsed.result.status}'))
            continue
        parsed_versions.append((version, gathered, parsed, source_dir))

    overrides = selected_bib_files or {}
    items = []
    read_failed: list[dict] = []
    for version, gathered, parsed, source_dir in parsed_versions:
        chosen = overrides.get((arxiv_id, version))
        if chosen is not None:
            chosen = tuple(chosen)
        try:
            texts = _source_texts(gathered, source_dir)
        except SourceTextUnavailable as exc:
            # Cannot verify source spans without the decoded A1 text: downgrade
            # instead of silently bypassing provenance verification.
            read_failed.append(dict(
                version=version, status='partial',
                reason='source_texts_unavailable',
                message=f'Cannot decode source text for provenance verification: {exc}',
            ))
            stats['versions_partial'] += 1
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version, status='parse_warning',
                stage='source_verify', code='source_texts_unavailable',
                message=f'Cannot decode source text for provenance verification: {exc}',
                path=None, start=None, end=None, spans=[],
            ))
            continue
        items.append(VersionInput(
            hierarchy=parsed.result,
            gathered=gathered.raw,
            source_dir=source_dir,
            source_texts=texts,
            selected_bib_files=chosen,
        ))
    approved = ()
    if approved_metadata_candidates:
        approved = tuple(approved_metadata_candidates.get(arxiv_id, ()))

    try:
        standardized = services.standardize_publication(
            arxiv_id, items, node_factory=TreeNode,
            include_partial=include_partial,
            approved_metadata_candidates=approved,
        )
    except Exception as exc:
        read_failed_versions = {item['version'] for item in read_failed}
        for version, _, _, _ in parsed_versions:
            if version in read_failed_versions:
                continue
            stats['versions_failed'] += 1
            errors.append(dict(arxiv_id=arxiv_id, version=version,
                               status='failed',
                               error=f'standardize:{type(exc).__name__}:{exc}'))
        audit = _standardization_audit(
            arxiv_id, None, services, warnings, errors, read_failed=read_failed)
        return stats, errors, warnings, rows, audit

    seen_versions = set()
    for version in getattr(standardized, 'versions', ()) or ():
        seen_versions.add(version.version)
        status = str(version.status)
        for diagnostic in getattr(getattr(version, 'normalized', None), 'diagnostics', ()) or ():
            warnings.append(_diagnostic_warning(arxiv_id, version.version, 'normalize', diagnostic))
        bibliography = getattr(version, 'bibliography', None)
        for diagnostic in getattr(bibliography, 'selection_diagnostics', ()) or ():
            warnings.append(_diagnostic_warning(arxiv_id, version.version,
                                                'bibliography', diagnostic))
        for resource in getattr(bibliography, 'resources', ()) or ():
            if getattr(resource, 'diagnostic', None) is not None:
                warnings.append(_diagnostic_warning(
                    arxiv_id, version.version, 'bibliography_read',
                    resource.diagnostic, path=getattr(resource, 'path', None)))
            parsed_resource = getattr(resource, 'parsed', None)
            for diagnostic in getattr(parsed_resource, 'warnings', ()) or ():
                warnings.append(_diagnostic_warning(
                    arxiv_id, version.version, 'bibliography_parse',
                    diagnostic, path=getattr(resource, 'path', None)))
        finalized = getattr(version, 'finalized', None)
        tree = getattr(finalized, 'tree', None) if finalized is not None else None
        if tree is not None:
            store.add_tree(version.version, tree)
            stats['versions_parsed'] += 1
        elif status == 'partial' and not include_partial:
            stats['versions_partial'] += 1
        else:
            stats['versions_pending'] += 1
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version.version, status='parse_warning',
                stage='standardize', code='no_finalized_tree',
                message='Version has no finalized tree; element rows are skipped.',
                path=None, start=None, end=None, spans=[],
            ))

    for version, _, _, _ in parsed_versions:
        if version in read_failed:
            continue
        if version not in seen_versions:
            stats['versions_pending'] += 1
            warnings.append(dict(
                arxiv_id=arxiv_id, version=version, status='parse_warning',
                stage='standardize', code='version_not_returned',
                message='Standardization did not return this version.',
                path=None, start=None, end=None, spans=[],
            ))

    references = getattr(standardized, 'references', None)
    for finding in getattr(references, 'findings', ()) or ():
        code = str(getattr(finding, 'code', 'reference_finding'))
        if code in ('ambiguous_old_key', 'ambiguous_citation_mapping'):
            code = 'ambiguous_reference_key'
        warnings.append(dict(
            arxiv_id=arxiv_id, version=None, status='parse_warning',
            stage='references', code=code,
            message=str(getattr(finding, 'message', '')),
            severity='warning', path=None, start=None, end=None, spans=[],
        ))

    merge = getattr(references, 'merge', None)
    records_found = getattr(merge, 'records', ()) if merge is not None else ()
    occurrences = getattr(merge, 'occurrences', ()) if merge is not None else ()
    unresolved = getattr(merge, 'unresolved', ()) if merge is not None else ()
    for record in records_found:
        payload = record.payload.as_kwargs()
        rows['bibtex'].append({name: payload.get(name) for name in BIBTEX_SCHEMA.names})
    rows['bibtex'].sort(key=lambda row: row['citation_key'])
    stats['bibtex_before'] = len(occurrences)
    stats['bibtex_after'] = len(records_found)
    # Dedup only counts merged groups with more than one member. Occurrences
    # with no exportable payload (invalid original key) are tracked separately
    # as unexportable, never as duplicates.
    stats['bibtex_duplicates'] = sum(max(0, len(getattr(record, 'members', ())) - 1)
                                     for record in records_found)
    stats['bibtex_unexportable'] = sum(1 for item in occurrences
                                       if getattr(getattr(item, 'prepared', None),
                                                  'payload', None) is None)
    stats['bibtex_unresolved'] = len(unresolved)
    stats['bibtex_conflicts'] = sum(1 for record in records_found
                                    if getattr(record, 'conflicts', ()))
    stats['bibtex_conflict_fields'] = sum(len(getattr(record, 'conflicts', ()))
                                          for record in records_found)

    rows['elements'] = element_rows(store.sorted_elements())
    rows['hierarchy'] = hierarchy_rows(store.sorted_edges())
    validation = validate_or_raise(arxiv_id, rows)
    warnings.extend(validation['warnings'])
    stats['citations_unresolved'] = len({item['citation_key'] for item in validation['warnings']
                                         if item.get('code') == 'unresolved_citation'})
    stats['elements_before'] = store.total_nodes
    stats['elements_after'] = store.distinct_elements
    audit = _standardization_audit(
        arxiv_id, standardized, services, warnings, errors, read_failed=read_failed)
    return stats, errors, warnings, rows, audit


def _process_publication(arxiv_id: str, records, services: ParserServices, *,
                         include_partial: bool = False,
                         selected_bib_files=None,
                         approved_metadata_candidates=None):
    if callable(getattr(services, 'standardize_publication', None)):
        return _process_publication_b(
            arxiv_id, records, services, include_partial=include_partial,
            selected_bib_files=selected_bib_files,
            approved_metadata_candidates=approved_metadata_candidates)
    stats, errors, warnings, rows = _process_publication_legacy(
        arxiv_id, records, services, include_partial=include_partial)
    return stats, errors, warnings, rows, _legacy_audit(arxiv_id, warnings, errors)


def _write_shards(parts_root: Path, arxiv_id: str, rows: dict[str, list[dict]],
                  audit: dict) -> None:
    encoded = quote(arxiv_id, safe='')
    part_dir = parts_root / encoded
    temporary = parts_root / f'{encoded}.tmp'
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True, exist_ok=True)
    for name, schema in TABLES:
        table = pa.Table.from_pylist(rows[name], schema=schema)
        pq.write_table(table, temporary / f'{name}.parquet')
    _atomic_json(temporary / 'audit.json', audit)
    if part_dir.exists():
        shutil.rmtree(part_dir)
    temporary.replace(part_dir)


def _audit_is_valid(path: Path, arxiv_id: str) -> bool:
    payload = _read_json(path, None)
    return (isinstance(payload, dict) and payload.get('schema_version') == 1
            and payload.get('arxiv_id') == arxiv_id)


def _shards_exist(parts_root: Path, arxiv_id: str) -> bool:
    part_dir = parts_root / quote(arxiv_id, safe='')
    return (all((part_dir / f'{name}.parquet').is_file() for name, _ in TABLES)
            and _audit_is_valid(part_dir / 'audit.json', arxiv_id))


def _publication_audits(parts_root: Path, arxiv_ids) -> dict:
    audits = {}
    for arxiv_id in sorted(arxiv_ids):
        path = parts_root / quote(arxiv_id, safe='') / 'audit.json'
        payload = _read_json(path, None)
        if isinstance(payload, dict):
            audits[arxiv_id] = payload
    return audits


def _merge_parts(parts_root: Path, output_root: Path, arxiv_ids) -> dict[str, int]:
    part_dirs = [parts_root / quote(arxiv_id, safe='') for arxiv_id in sorted(arxiv_ids)]
    counts = {}
    for name, schema in TABLES:
        final = output_root / f'{name}.parquet'
        writer = pq.ParquetWriter(final, schema, compression='snappy')
        total = 0
        try:
            for part_dir in part_dirs:
                part = part_dir / f'{name}.parquet'
                if not part.is_file():
                    continue
                table = pq.ParquetFile(part).read()
                total += table.num_rows
                writer.write_table(table)
        finally:
            writer.close()
        counts[name] = total
    return counts


def _write_coverage(output_root: Path, processed: dict) -> None:
    columns = ['arxiv_id', 'versions_total', 'versions_parsed', 'versions_empty',
               'versions_partial', 'versions_pending', 'versions_failed', 'missing',
               'gather_failed', 'elements_before', 'elements_after', 'bibtex_before',
               'bibtex_after', 'bibtex_duplicates', 'bibtex_unexportable',
               'bibtex_unresolved', 'citations_unresolved',
               'bibtex_conflicts', 'bibtex_conflict_fields', 'seconds']
    path = output_root / 'coverage.csv'
    with path.open('w', newline='', encoding='utf8') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for arxiv_id in sorted(processed):
            stats = processed[arxiv_id]['stats']
            writer.writerow(dict(arxiv_id=arxiv_id, **{name: stats.get(name, 0) for name in columns[1:]}))


def _software_versions() -> dict:
    try:
        import pyarrow
        pyarrow_version = pyarrow.__version__
    except Exception:
        pyarrow_version = None
    return dict(python=platform.python_version(), pyarrow=pyarrow_version,
                parser=PARSER_VERSION, platform=platform.platform())


def _max_rss_bytes() -> int | None:
    """Best-effort process peak RSS; uses resource where available (Linux/Colab)."""
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if platform.system() == 'Darwin' else usage * 1024


def _tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def run_parser(persistent_root: str | Path = 'runtime', *,
               output_root: str | Path | None = None,
               limit: int | None = None,
               resume: bool = True,
               log=None,
               services: ParserServices | None = None,
               include_partial: bool = False,
               selected_bib_files: dict | None = None,
               approved_metadata_candidates: dict | None = None) -> dict:
    """Parse all Milestone 1 publications and write the three M2 tables.

    ``services`` injects the A1/A2/A3/B1/B2/B3 boundary. When omitted, the real
    components are loaded from :mod:`parser.integration`.
    """
    root = Path(persistent_root)
    output = resolve_output(root, output_root)
    _guard_output(root, output)
    output.mkdir(parents=True, exist_ok=True)
    parts_root = output / '_parts'
    checkpoint_path = output / 'checkpoint.json'

    if not resume:
        if parts_root.exists():
            shutil.rmtree(parts_root)
        checkpoint_path.unlink(missing_ok=True)
    parts_root.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    publications = publications_with_records(root, limit=limit)
    input_ids = [arxiv_id for arxiv_id, _ in publications]
    if services is None and publications:
        services = load_services()

    # A service without a reliable fingerprint cannot prove that cached shards
    # match its implementation or runtime configuration. Never reuse them:
    # reprocess instead of silently serving stale output.
    resume_effective = resume
    resume_disabled_reason = None
    if (resume_effective and services is not None
            and not callable(getattr(services, 'cache_fingerprint', None))):
        resume_effective = False
        resume_disabled_reason = 'service_without_cache_fingerprint'
        if log:
            log('resume disabled: injected service has no cache_fingerprint(); '
                'reprocessing all publications')

    policy_fingerprint = _policy_fingerprint(include_partial, services)
    checkpoint = _read_json(checkpoint_path, {'schema_version': CHECKPOINT_SCHEMA,
                                              'policy_fingerprint': policy_fingerprint,
                                              'processed': {}, 'errors': []})
    if (checkpoint.get('schema_version') != CHECKPOINT_SCHEMA
            or checkpoint.get('policy_fingerprint') != policy_fingerprint):
        checkpoint = {'schema_version': CHECKPOINT_SCHEMA,
                      'policy_fingerprint': policy_fingerprint,
                      'processed': {}, 'errors': []}
    selected = set(input_ids)
    processed: dict = {arxiv_id: item for arxiv_id, item in checkpoint.get('processed', {}).items()
                       if arxiv_id in selected}

    dirty = False
    flush_every = 25
    for index, (arxiv_id, records) in enumerate(publications, start=1):
        input_fingerprint = _publication_input_fingerprint(
            arxiv_id, records, selected_bib_files=selected_bib_files,
            approved_metadata_candidates=approved_metadata_candidates)
        if (resume_effective and arxiv_id in processed
                and _shards_exist(parts_root, arxiv_id)
                and processed[arxiv_id].get('input_fingerprint') == input_fingerprint
                and processed[arxiv_id].get('policy_fingerprint') == policy_fingerprint):
            if log:
                log(f'[{index}/{len(publications)}] skip {arxiv_id} (checkpoint)')
            continue
        if log:
            log(f'[{index}/{len(publications)}] parse {arxiv_id} ({len(records)} versions)')
        publication_started = time.perf_counter()
        stats, errors, warnings, rows, audit = _process_publication(
            arxiv_id, records, services, include_partial=include_partial,
            selected_bib_files=selected_bib_files,
            approved_metadata_candidates=approved_metadata_candidates,
        )
        stats['seconds'] = round(time.perf_counter() - publication_started, 3)
        _write_shards(parts_root, arxiv_id, rows, audit)
        processed[arxiv_id] = dict(stats=stats, errors=errors, warnings=warnings,
                                   input_fingerprint=input_fingerprint,
                                   policy_fingerprint=policy_fingerprint)
        dirty = True
        if index % flush_every == 0 or index == len(publications):
            checkpoint['processed'] = processed
            checkpoint['errors'] = [error for item in processed.values() for error in item.get('errors', [])]
            _atomic_json(checkpoint_path, checkpoint)
            dirty = False
    if dirty:
        checkpoint['processed'] = processed
        checkpoint['errors'] = [error for item in processed.values() for error in item.get('errors', [])]
        _atomic_json(checkpoint_path, checkpoint)

    counts = _merge_parts(parts_root, output, input_ids)
    _write_coverage(output, processed)
    audits = _publication_audits(parts_root, input_ids)
    _atomic_json(output / 'bibtex_audit.json',
                 dict(schema_version=1, publications=audits))
    errors = [error for item in processed.values() for error in item.get('errors', [])]
    warnings = [warning for item in processed.values() for warning in item.get('warnings', [])]
    _atomic_json(output / 'errors.json', errors)
    output_validation = validate_output(output)

    elapsed = time.perf_counter() - started
    aggregate = _version_stats()
    for item in processed.values():
        for name in aggregate:
            aggregate[name] += item['stats'].get(name, 0)

    input_fingerprints = {arxiv_id: item.get('input_fingerprint')
                          for arxiv_id, item in processed.items()}
    config_payload = dict(
        persistent_root=str(root), output_root=str(output), limit=limit,
        resume=resume, include_partial=include_partial,
        selected_bib_overrides=len(selected_bib_files or {}),
        approved_metadata_publications=len(approved_metadata_candidates or {}),
    )
    manifest = {
        'schema_version': MANIFEST_SCHEMA,
        'parser_version': PARSER_VERSION,
        'input_fingerprints': input_fingerprints,
        'parser_policy': dict(include_partial=include_partial,
                              policy_fingerprint=policy_fingerprint,
                              input_fingerprints=input_fingerprints),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'started_at': started_at.isoformat(),
        'resume': dict(requested=resume, effective=resume_effective,
                       disabled_reason=resume_disabled_reason),
        'read_only_m1': True,
        'persistent_root': str(root),
        'output_root': str(output),
        'limit': limit,
        'papers_total': len(publications),
        'papers_processed': len(processed),
        'versions': {key: aggregate[key] for key in (
            'versions_total', 'versions_parsed', 'versions_empty',
            'versions_partial', 'versions_pending', 'versions_failed',
            'missing', 'gather_failed')},
        'elements': dict(before_dedup=aggregate['elements_before'],
                         after_dedup=aggregate['elements_after'],
                         deduplicated=aggregate['elements_before'] - aggregate['elements_after']),
        'bibtex': dict(before_dedup=aggregate['bibtex_before'],
                       after_dedup=aggregate['bibtex_after'],
                       deduplicated=aggregate['bibtex_duplicates'],
                       unexportable=aggregate['bibtex_unexportable'],
                       unresolved=aggregate['bibtex_unresolved']),
        'quality': dict(citations_unresolved=aggregate['citations_unresolved'],
                        bibtex_conflicts=aggregate['bibtex_conflicts'],
                        bibtex_conflict_fields=aggregate['bibtex_conflict_fields'],
                        bibtex_unexportable=aggregate['bibtex_unexportable'],
                        bibtex_unresolved=aggregate['bibtex_unresolved']),
        'audit': dict(schema_version=1, publications=len(audits),
                      path='bibtex_audit.json'),
        'empty_versions': aggregate['versions_empty'],
        'timing': dict(elapsed_seconds=round(elapsed, 3),
                       average_seconds_per_publication=round(elapsed / len(processed), 3)
                       if processed else None),
        'memory': dict(peak_rss_bytes=_max_rss_bytes()),
        'storage': dict(output_bytes=None, parts_bytes=_tree_bytes(parts_root)),
        'errors': errors,
        'validation': dict(
            ok=output_validation['ok'],
            errors=output_validation['errors'],
            warnings=warnings,
            publications_checked=len(processed),
            rows={name: counts[name] for name, _ in TABLES},
        ),
        'fingerprints': dict(
            config=_sha256_text(json.dumps(config_payload, sort_keys=True)),
            input=_sha256_text(json.dumps(sorted(input_ids), sort_keys=True)),
            schema=_sha256_text(str(ELEMENTS_SCHEMA) + str(HIERARCHY_SCHEMA) + str(BIBTEX_SCHEMA)),
        ),
        'software': _software_versions(),
        'outputs': {},
    }
    for name, _ in TABLES:
        path = output / f'{name}.parquet'
        manifest['outputs'][name] = dict(rows=counts[name], sha256=digest(path),
                                         bytes=path.stat().st_size)
    coverage_path = output / 'coverage.csv'
    manifest['outputs']['coverage.csv'] = dict(rows=len(processed), sha256=digest(coverage_path),
                                               bytes=coverage_path.stat().st_size)
    audit_path = output / 'bibtex_audit.json'
    manifest['outputs']['bibtex_audit.json'] = dict(
        rows=len(audits), sha256=digest(audit_path), bytes=audit_path.stat().st_size)
    manifest['storage']['output_bytes'] = sum(item['bytes'] for item in manifest['outputs'].values())
    _atomic_json(output / 'manifest.json', manifest)
    if not output_validation['ok']:
        raise ValidationError(f'output validation failed: {output_validation["errors"][0]}')
    return manifest
