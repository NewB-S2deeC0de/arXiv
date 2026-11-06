"""Offsets are Unicode character offsets into input raw; end is exclusive."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    start: int
    end: int


@dataclass(frozen=True)
class Segment:
    kind: str
    raw: str
    start: int
    end: int


@dataclass(frozen=True)
class ScanResult:
    segments: tuple[Segment, ...]
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class NormalizationResult:
    content: str
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class SourceContext:
    arxiv_id: str | None = None
    version: int | None = None
    source_file: str | None = None

    def __post_init__(self):
        for value in (self.arxiv_id, self.source_file):
            if value is not None and not isinstance(value, str):
                raise TypeError('arxiv_id/source_file must be strings or None')
        if self.version is not None and (type(self.version) is not int or self.version < 1):
            raise ValueError('version must be a positive integer or None')


@dataclass(frozen=True)
class RawFragment:
    kind: str
    raw: str
    start: int
    end: int
    region_index: int


@dataclass(frozen=True)
class BibItem:
    citation_key: str | None
    citation_key_raw: str | None
    optional_label: str | None
    raw: str
    raw_content: str
    start: int
    end: int
    content_start: int
    region_index: int
    source: SourceContext | None = None
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class BibliographyRegion:
    raw: str
    start: int
    end: int
    body_start: int
    body_end: int
    closed: bool
    header_valid: bool
    safe_to_exclude: bool
    entry_indexes: tuple[int, ...]
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class BibliographyResult:
    raw: str
    regions: tuple[BibliographyRegion, ...]
    entries: tuple[BibItem, ...]
    unparsed: tuple[RawFragment, ...]
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def exclusion_spans(self) -> tuple[tuple[int, int], ...]:
        """Only structurally safe bibliography regions, never an edited body."""
        return tuple((region.start, region.end) for region in self.regions
                     if region.safe_to_exclude)
