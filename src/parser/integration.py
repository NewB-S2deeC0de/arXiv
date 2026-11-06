"""Boundary between the C3 pipeline and the A/B parsing components.

The pipeline owns orchestration only. A1/A2/A3/B1/B2/B3 are separate issues and
their modules are loaded here lazily so C3 tests can inject fakes while those
components are still being integrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

from .contract import (BibliographyBlock, GatherDiagnostic, GatheredSource,
                       HierarchyResult, ParseDiagnostic, ParseNode,
                       ParsedDocument, ParserServices, SourceSpan, VersionInput)


class ParserDependencyError(RuntimeError):
    """Raised when the A/B parsing components are not available yet."""

    def __init__(self, message: str, *, code: str = 'missing_module'):
        super().__init__(message)
        self.code = code


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parser_module_fingerprint() -> dict[str, str]:
    """Hash every parser source file so A/B/C code changes invalidate resume."""
    base = Path(__file__).resolve().parent
    fingerprints = {}
    for path in sorted(base.glob('*.py')):
        try:
            fingerprints[path.name] = _sha256_bytes(path.read_bytes())
        except OSError:
            fingerprints[path.name] = 'unreadable'
    return fingerprints


def _to_span(span) -> SourceSpan:
    return SourceSpan(path=str(span.path), start=int(span.start), end=int(span.end))


def _to_node(node) -> ParseNode:
    return ParseNode(
        occurrence_id=int(getattr(node, 'occurrence_id', 0)),
        kind=str(node.kind),
        raw=str(node.raw),
        source_spans=tuple(_to_span(s) for s in getattr(node, 'source_spans', ())),
        children=tuple(_to_node(child) for child in getattr(node, 'children', ())),
        attrs=dict(getattr(node, 'attrs', {})),
        content=getattr(node, 'content', None),
        warnings=tuple(getattr(node, 'warnings', ())),
    )


def _to_block(block) -> BibliographyBlock:
    return BibliographyBlock(
        raw=str(block.raw),
        source_spans=tuple(_to_span(s) for s in getattr(block, 'source_spans', ())),
    )


def _to_result(result) -> HierarchyResult:
    return HierarchyResult(
        arxiv_id=str(result.arxiv_id),
        version=int(result.version),
        status=str(result.status),
        root=_to_node(result.root) if result.root is not None else None,
        bibliography=tuple(_to_block(block) for block in getattr(result, 'bibliography', ())),
        diagnostics=tuple(_parse_diagnostic(item) for item in getattr(result, 'diagnostics', ())),
    )


def _parse_diagnostic(item) -> ParseDiagnostic:
    spans = tuple(_to_span(s) for s in getattr(item, 'source_spans', ()))
    return ParseDiagnostic(code=str(item.code), message=str(item.message),
                           severity=str(getattr(item, 'severity', 'warning')),
                           source_spans=spans)


def _parser_fingerprint() -> str:
    return _sha256_text(json.dumps(_parser_module_fingerprint(), sort_keys=True))


@dataclass(frozen=True)
class DefaultParserServices:
    """Adapters over the A1/A2/A3/B1/B2/B3 implementations."""

    def gather(self, arxiv_id: str, version: int, source_dir: Path) -> GatheredSource:
        from .gather import gather_version

        gathered = gather_version(arxiv_id, version, Path(source_dir))
        diagnostics = tuple(
            GatherDiagnostic(
                code=item.code,
                message=item.message,
                severity=item.severity,
                path=item.origin.path if item.origin else None,
                start=item.origin.start if item.origin else None,
                end=item.origin.end if item.origin else None,
            )
            for item in gathered.diagnostics
        )
        return GatheredSource(
            text=''.join(segment.text for segment in gathered.segments),
            status=gathered.status,
            root_file=gathered.root_file,
            files=tuple(gathered.used_tex_files),
            bib_files=tuple(gathered.bib_files),
            diagnostics=diagnostics,
            raw=gathered,
        )

    def parse_document(self, gathered: GatheredSource, arxiv_id: str,
                       version: int) -> ParsedDocument:
        if gathered.raw is None:
            raise ParserDependencyError(
                'A2 requires the raw A1 GatheredVersion. '
                'Use DefaultParserServices.gather() or inject a test service.'
            )
        from .hierarchy import build_hierarchy

        try:
            from .leaves import refine_hierarchy
        except ImportError as exc:
            raise ParserDependencyError(
                'A3 refine_hierarchy is not available yet. '
                'Merge A3/B1 or pass services=... to run_parser().'
            ) from exc

        a2_result = build_hierarchy(gathered.raw)
        a3_result = refine_hierarchy(a2_result)
        return ParsedDocument(result=_to_result(a3_result))

    def standardize_publication(self, arxiv_id: str, versions: list[VersionInput],
                                *, node_factory, include_partial: bool = False,
                                approved_metadata_candidates=()):
        try:
            from standardization import (PublicationVersionInput,
                                          standardize_publication as run)
        except ImportError as exc:
            raise ParserDependencyError(
                'B standardization API is not available. '
                'Merge B1/B2/B3 or pass services=... to run_parser().',
                code='missing_module',
            ) from exc
        items = [
            PublicationVersionInput(
                hierarchy=item.hierarchy,
                gathered=item.gathered,
                source_dir=Path(item.source_dir),
                source_texts=item.source_texts,
                selected_bib_files=item.selected_bib_files,
            )
            for item in versions
        ]
        return run(arxiv_id, items, node_factory=node_factory,
                   include_partial=include_partial,
                   approved_metadata_candidates=approved_metadata_candidates)

    def audit_payload(self, standardized) -> dict:
        from standardization import b3_audit_payload

        return b3_audit_payload(standardized.references)

    def cache_fingerprint(self) -> str:
        """Fingerprint A/B/C code and policy so cached shards never go stale.

        The parser hash covers A1/A2/A3 and the C adapters; B composes its own
        source and policy hash through ``b3_cache_fingerprint``.
        """
        parser_fingerprint = _parser_fingerprint()
        try:
            from standardization import b3_cache_fingerprint
        except ImportError:
            return parser_fingerprint
        return b3_cache_fingerprint(parser_fingerprint=parser_fingerprint)


def load_services() -> ParserServices:
    """Load the real A/B implementations, with a clear error while unmerged."""
    try:
        from . import gather, hierarchy, leaves  # noqa: F401
        from .leaves import refine_hierarchy  # noqa: F401
    except ImportError as exc:
        raise ParserDependencyError(
            'A1/A2/A3 parser modules are not available. '
            'Merge those components or pass services=... to run_parser().',
            code='missing_module',
        ) from exc
    try:
        from standardization import (PublicationVersionInput, b3_audit_payload,
                                      b3_cache_fingerprint,
                                      standardize_publication)
    except ImportError as exc:
        raise ParserDependencyError(
            'B1/B2/B3 standardization modules are not available. '
            'Merge group B or pass services=... to run_parser().',
            code='missing_module',
        ) from exc
    if not all(callable(item) for item in (
            refine_hierarchy, standardize_publication, b3_audit_payload,
            b3_cache_fingerprint)):
        raise ParserDependencyError(
            'B standardization API is incomplete or not wired.',
            code='not_wired',
        )
    return DefaultParserServices()


def capabilities() -> dict:
    """Report which integration components are importable and wired."""
    result = dict(gather=False, build_hierarchy=False, refine_hierarchy=False,
                  standardize=False, missing=[])
    try:
        from . import gather, hierarchy, leaves  # noqa: F401
        result['gather'] = callable(getattr(gather, 'gather_version', None))
        result['build_hierarchy'] = callable(getattr(hierarchy, 'build_hierarchy', None))
        result['refine_hierarchy'] = callable(getattr(leaves, 'refine_hierarchy', None))
    except ImportError as exc:
        result['missing'].append(f'parser:{exc.name}')
    try:
        from standardization import standardize_publication
        result['standardize'] = callable(standardize_publication)
    except ImportError as exc:
        result['missing'].append(f'standardization:{exc.name}')
    return result
