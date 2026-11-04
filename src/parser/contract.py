"""Typed contracts for Milestone 2 parsing (issue C1).

The three published tables are ``elements``, ``hierarchy`` and ``bibtex``.
All identifiers are strings; nullable columns keep ``None`` instead of empty
strings so downstream consumers can distinguish missing data from empty text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import pyarrow as pa


class ElementKind(StrEnum):
    DOCUMENT = 'document'
    HEADING = 'heading'
    PARAGRAPH = 'paragraph'
    LIST = 'list'
    ITEM = 'item'
    SENTENCE = 'sentence'
    BLOCK_FORMULA = 'block_formula'
    FIGURE = 'figure'


# Short discriminant used inside a publication-scoped element id.
KIND_PREFIX = {
    ElementKind.DOCUMENT: 'root',
    ElementKind.HEADING: 'h',
    ElementKind.PARAGRAPH: 'p',
    ElementKind.LIST: 'l',
    ElementKind.ITEM: 'i',
    ElementKind.SENTENCE: 's',
    ElementKind.BLOCK_FORMULA: 'm',
    ElementKind.FIGURE: 'f',
}

#: The specification treats tables as a kind of figure, so both map here.
FIGURE_ENVIRONMENTS = ('figure', 'figure*', 'table', 'table*', 'algorithm', 'algorithm*')


class SourceStatus(StrEnum):
    SUCCEEDED = 'succeeded'
    NO_TEX = 'no_tex'
    FAILED = 'failed'
    MISSING = 'missing'


@dataclass(frozen=True)
class SourceRecord:
    """One versioned source unit discovered in the Milestone 1 runtime."""

    arxiv_id: str
    version: int
    status: SourceStatus
    source_path: Path | None = None
    tex_files: tuple[Path, ...] = ()
    bib_files: tuple[Path, ...] = ()
    error: str | None = None

    @property
    def has_tex(self) -> bool:
        return bool(self.tex_files)


class GatherError(ValueError):
    """Shared failure type for source gathering (A1)."""


@dataclass(frozen=True)
class SourceSpan:
    """Relative file path and [start, end) Unicode offsets on decoded file text."""

    path: str
    start: int
    end: int


@dataclass(frozen=True)
class GatherDiagnostic:
    """Normalized A1 diagnostic consumed by the C3 pipeline."""

    code: str
    message: str
    severity: str = 'warning'
    path: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class GatheredSource:
    """Output of A1 source gathering, before hierarchy construction."""

    text: str
    status: str = 'ready'
    root_file: str | None = None
    files: tuple[str, ...] = ()
    bib_files: tuple[str, ...] = ()
    diagnostics: tuple[GatherDiagnostic, ...] = ()
    raw: object | None = field(default=None, repr=False, compare=False)


@dataclass
class TreeNode:
    """Shared tree node produced by A2/A3 and consumed by C2/C3."""

    kind: str
    content: str
    children: list['TreeNode'] = field(default_factory=list)


@dataclass(frozen=True)
class BibliographyBlock:
    """Raw bibliography block kept by A2/A3 with provenance spans."""

    raw: str
    source_spans: tuple[SourceSpan, ...] = ()


@dataclass(frozen=True)
class ParseNode:
    """Rich A2/A3 tree node; raw/provenance immutable, content filled by B1."""

    occurrence_id: int
    kind: str
    raw: str
    source_spans: tuple[SourceSpan, ...] = ()
    children: tuple['ParseNode', ...] = ()
    attrs: dict = field(default_factory=dict)
    content: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParseDiagnostic:
    """Normalized A2/A3 diagnostic consumed by the C3 pipeline."""

    code: str
    message: str
    severity: str = 'warning'
    source_spans: tuple[SourceSpan, ...] = ()

    @property
    def path(self) -> str | None:
        return self.source_spans[0].path if self.source_spans else None

    @property
    def start(self) -> int | None:
        return self.source_spans[0].start if self.source_spans else None

    @property
    def end(self) -> int | None:
        return self.source_spans[0].end if self.source_spans else None


@dataclass(frozen=True)
class HierarchyResult:
    """A2/A3 hierarchy result retained by C3 until B1/C3 finalize content."""

    arxiv_id: str
    version: int
    status: str
    root: ParseNode | None = None
    bibliography: tuple[BibliographyBlock, ...] = ()
    diagnostics: tuple[ParseDiagnostic, ...] = ()


@dataclass(frozen=True)
class ParsedDocument:
    """A2/A3 hierarchy result plus optional C2-ready TreeNode after normalization."""

    result: HierarchyResult
    tree: TreeNode | None = None


@dataclass(frozen=True)
class Element:
    element_id: str
    arxiv_id: str
    kind: str
    content: str


@dataclass(frozen=True)
class HierarchyEdge:
    arxiv_id: str
    version: int
    element_id: str
    parent_id: str


@dataclass(frozen=True)
class BibEntry:
    arxiv_id: str
    citation_key: str
    entry_type: str = 'misc'
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    journal: str | None = None
    url: str | None = None


class ParserServices(Protocol):
    """A1/A2/A3/B1/B2/B3 boundary consumed by the C3 pipeline."""

    def gather(self, arxiv_id: str, version: int, source_dir: Path) -> GatheredSource:
        ...

    def parse_document(self, gathered: GatheredSource, arxiv_id: str,
                       version: int) -> ParsedDocument:
        ...

    def extract_bibitems(self, text: str, arxiv_id: str) -> list[BibEntry]:
        ...

    def unify_bibentries(self, versioned: list[tuple[int, BibEntry]]
                         ) -> tuple[list[BibEntry], dict[str, str], dict]:
        ...

    def remap_citations(self, text: str, mapping: dict[str, str]) -> str:
        ...


ELEMENTS_SCHEMA = pa.schema([
    pa.field('element_id', pa.string(), nullable=False),
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('kind', pa.string()),
    pa.field('content', pa.string()),
])

HIERARCHY_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('version', pa.int64()),
    pa.field('element_id', pa.string(), nullable=False),
    pa.field('parent_id', pa.string(), nullable=False),
])

BIBTEX_SCHEMA = pa.schema([
    pa.field('arxiv_id', pa.string(), nullable=False),
    pa.field('citation_key', pa.string(), nullable=False),
    pa.field('entry_type', pa.string()),
    pa.field('title', pa.string()),
    pa.field('authors', pa.list_(pa.string())),
    pa.field('year', pa.int64()),
    pa.field('doi', pa.string()),
    pa.field('journal', pa.string()),
    pa.field('url', pa.string()),
])

TABLES = {
    'elements': ELEMENTS_SCHEMA,
    'hierarchy': HIERARCHY_SCHEMA,
    'bibtex': BIBTEX_SCHEMA,
}


def element_rows(elements: list[Element]) -> list[dict]:
    return [dict(element_id=e.element_id, arxiv_id=e.arxiv_id,
                 kind=e.kind, content=e.content) for e in elements]


def hierarchy_rows(edges: list[HierarchyEdge]) -> list[dict]:
    return [dict(arxiv_id=e.arxiv_id, version=e.version,
                 element_id=e.element_id, parent_id=e.parent_id) for e in edges]


def bibtex_rows(entries: list[BibEntry]) -> list[dict]:
    return [dict(arxiv_id=e.arxiv_id, citation_key=e.citation_key,
                 entry_type=e.entry_type, title=e.title,
                 authors=list(e.authors), year=e.year,
                 doi=e.doi, journal=e.journal, url=e.url) for e in entries]


def publication_key(arxiv_id: str) -> str:
    """Publication key used as prefix of element ids, e.g. 2310.12345 -> 2310-12345."""
    return arxiv_id.replace('/', '-').replace('.', '-')
