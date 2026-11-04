"""Boundary between the C3 pipeline and the A/B parsing components.

The pipeline owns orchestration only. A1/A2/A3/B1/B2/B3 are separate issues and
their modules are loaded here lazily so C3 tests can inject fakes while those
components are still being integrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contract import (BibEntry, BibliographyBlock, GatherDiagnostic,
                       GatheredSource, HierarchyResult, ParseDiagnostic,
                       ParseNode, ParsedDocument, ParserServices, SourceSpan,
                       TreeNode)


class ParserDependencyError(RuntimeError):
    """Raised when the A/B parsing components are not available yet."""


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


def _as_tree(node, diagnostics: list[ParseDiagnostic]) -> TreeNode:
    """Convert a rich node to C2 TreeNode only after content is finalized."""
    for warning in getattr(node, 'warnings', ()):
        diagnostics.append(ParseDiagnostic('node_warning', str(warning),
                                           source_spans=tuple(
                                               _to_span(s) for s in getattr(node, 'source_spans', ()))))
    content = getattr(node, 'content', None)
    if content is None:
        content = getattr(node, 'raw', '')
    return TreeNode(
        kind=str(node.kind),
        content=content,
        children=[_as_tree(child, diagnostics) for child in getattr(node, 'children', ())],
    )


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
        from . import hierarchy

        if gathered.raw is None:
            raise ParserDependencyError(
                'A2 requires the raw A1 GatheredVersion. '
                'Use DefaultParserServices.gather() or inject a test service.'
            )
        if not hasattr(hierarchy, 'build_hierarchy'):
            raise ParserDependencyError(
                'A2 build_hierarchy is not available yet. '
                'Merge A2/A3/B1 or pass services=... to run_parser().'
            )
        a2_result = hierarchy.build_hierarchy(gathered.raw)
        result = a2_result
        if hasattr(hierarchy, 'refine_hierarchy'):
            result = hierarchy.refine_hierarchy(a2_result)
        return ParsedDocument(result=_to_result(result))

    def extract_bibitems(self, text: str, arxiv_id: str) -> list[BibEntry]:
        from .bibtex import extract_bibitems

        return extract_bibitems(text, arxiv_id)

    def unify_bibentries(self, versioned: list[tuple[int, BibEntry]]
                         ) -> tuple[list[BibEntry], dict[str, str], dict]:
        from .bibtex import unify_bibentries

        return unify_bibentries(versioned)

    def remap_citations(self, text: str, mapping: dict[str, str]) -> str:
        from .bibtex import remap_citations

        return remap_citations(text, mapping)


def load_services() -> ParserServices:
    """Load the real A/B implementations, with a clear error while unmerged."""
    try:
        from . import bibtex, gather, hierarchy  # noqa: F401
    except ImportError as exc:
        raise ParserDependencyError(
            'A1/A2/A3/B1/B2/B3 parser modules are not available. '
            'Merge those components or pass services=... to run_parser().'
        ) from exc
    return DefaultParserServices()
