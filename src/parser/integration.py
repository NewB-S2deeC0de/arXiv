"""Boundary between the C3 pipeline and the A/B parsing components.

The pipeline owns orchestration only. A1/A2/A3/B1/B2/B3 are separate issues and
their modules are loaded here lazily so C3 tests can inject fakes while those
components are still being integrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contract import (BibEntry, GatherDiagnostic, GatheredSource,
                       ParseDiagnostic, ParsedDocument, ParserServices,
                       TreeNode)


class ParserDependencyError(RuntimeError):
    """Raised when the A/B parsing components are not available yet."""


def _as_tree(node, diagnostics: list[ParseDiagnostic]) -> TreeNode:
    for warning in getattr(node, 'warnings', ()):
        diagnostics.append(ParseDiagnostic('node_warning', str(warning)))
    content = getattr(node, 'content', None)
    if content is None:
        content = getattr(node, 'raw', '')
    return TreeNode(
        kind=str(node.kind),
        content=content,
        children=[_as_tree(child, diagnostics) for child in node.children],
    )


def _parse_diagnostic(item) -> ParseDiagnostic:
    spans = getattr(item, 'source_spans', ())
    first = spans[0] if spans else None
    return ParseDiagnostic(
        code=item.code,
        message=item.message,
        severity=item.severity,
        path=first.path if first else None,
        start=first.start if first else None,
        end=first.end if first else None,
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
        from .hierarchy import build_hierarchy

        if gathered.raw is None:
            raise ParserDependencyError(
                'A2 requires the raw A1 GatheredVersion. '
                'Use DefaultParserServices.gather() or inject a test service.'
            )
        result = build_hierarchy(gathered.raw)
        diagnostics = [_parse_diagnostic(item) for item in result.diagnostics]
        tree = (_as_tree(result.root, diagnostics) if result.root is not None
                else TreeNode('document', ''))
        return ParsedDocument(tree=tree, status=result.status, diagnostics=tuple(diagnostics))

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
