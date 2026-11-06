"""Evidence-based metadata for the explicit IEEE quoted-title bibitem style.

Supports a documented subset; unrecognized prose remains raw staging data.
DOI/URL and C projection are handled by c_bibentry_adapter, not guessed here.
"""
from dataclasses import dataclass
import re

from .models import BibItem
from .models import Diagnostic
from .normalize import normalize_text
from .scanner import scan_latex, formatting_argument_span


@dataclass(frozen=True)
class BibFieldEvidence:
    field: str
    value: object
    raw: str
    start: int
    end: int
    rule: str


@dataclass(frozen=True)
class BibMetadataResult:
    entry: BibItem
    metadata: tuple[tuple[str, object], ...]
    evidence: tuple[BibFieldEvidence, ...]
    warnings: tuple[Diagnostic, ...]
    offset_basis: str = 'entry_raw_content'


_QUOTED_TITLE = re.compile(r"``(?P<title>.*?)''", re.S)
_YEAR = re.compile(r',\s*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+)?'
                   r'(?P<year>(?:18|19|20|21)[0-9]{2})(?=\s*(?:\.(?![0-9])|,\s*(?:pp?\.|publisher:)))', re.I)
_AUTHOR_SPACING = re.compile(r'\\BIBentry(?:ALTinterwordspacing|STDinterwordspacing)\b')


def extract_bibitem_metadata(entry):
    """Parse only explicit quoted-title/article-journal/year-clause evidence.

    Author lists require IEEE initial-first names with comma/and separators;
    et al. leaves authors unknown rather than inventing omitted authors.
    No reference entry_type is inferred from typography.
    """
    if not isinstance(entry, BibItem):
        raise TypeError('entry must be a B2 BibItem')
    raw = entry.raw_content
    scanned = scan_latex(raw)
    evidence, warnings, metadata = [], list(scanned.warnings), {}
    protected = [(s.start, s.end) for s in scanned.segments
                 if s.kind in {'comment', 'verbatim', 'url', 'citation', 'inline_math', 'block_math', 'command', 'malformed'}]

    def overlaps(start, end, regions):
        return any(left < end and start < right for left, right in regions)

    def add(field, value, start, end, rule):
        evidence.append(BibFieldEvidence(field, value, raw[start:end], start, end, rule))

    titles = [m for m in _QUOTED_TITLE.finditer(raw)
              if not overlaps(m.start(), m.start() + 2, protected)
              and not overlaps(m.end() - 2, m.end(), protected)]
    title = titles[0] if len(titles) == 1 else None
    if title:
        start, end = title.span('title')
        while start < end and raw[start].isspace():
            start += 1
        while end > start and raw[end - 1].isspace():
            end -= 1
        if end > start and raw[end - 1] == ',':
            end -= 1  # IEEE separator comma is printed inside closing quotes.
        value = normalize_text(raw[start:end])
        warnings.extend(Diagnostic(w.code, w.message, w.start + start, w.end + start) for w in value.warnings)
        if value.content.strip():
            metadata['title'] = value.content
            add('title', value.content, start, end, 'ieee_double_backtick_title')

        prefix = raw[:title.start()]
        author_text = normalize_text(_AUTHOR_SPACING.sub('', prefix)).content.replace('~', ' ').strip().rstrip(',').strip()
        if re.search(r'\bet\s+al\.', author_text, re.I):
            warnings.append(Diagnostic('authors_truncated',
                'et al. does not expose the full author list; authors remains unknown.', 0, title.start()))
        elif author_text and '\\' not in author_text and not overlaps(0, title.start(), [
                (s.start, s.end) for s in scanned.segments if (s.start, s.end) in protected
                and not _AUTHOR_SPACING.fullmatch(s.raw)]):
            authors = tuple(x.strip() for x in re.split(r',\s*(?:and\s+)?|\s+and\s+', author_text) if x.strip())
            if authors and all(re.match(r'[A-Z]\.', author) and re.search(r'[A-Za-z]{2}', author) for author in authors):
                metadata['authors'] = authors
                add('authors', authors, 0, title.start(), 'ieee_initial_first_author_list')
            else:
                warnings.append(Diagnostic('authors_style_deferred', 'Author grammar is outside supported initial-first style.', 0, title.start()))
        elif author_text:
            warnings.append(Diagnostic('authors_style_deferred', 'Author prefix contains protected/unknown constructs.', 0, title.start()))

        # A journal-style emph immediately after title; book-style "in emph"
        # is not assigned to journal. This is a narrow style rule, not a type inference.
        for segment in scanned.segments:
            if segment.start < title.end():
                continue
            gap = raw[title.end():segment.start]
            if segment.kind == 'formatting' and not gap.strip(' ,\t\r\n'):
                argument = formatting_argument_span(segment.raw)
                if argument and argument[0] == 'emph':
                    _, left, right = argument
                    start, end = segment.start + left, segment.start + right
                    content = normalize_text(raw[start:end])
                    warnings.extend(Diagnostic(w.code, w.message, w.start + start, w.end + start) for w in content.warnings)
                    if content.content.strip():
                        if re.match(r'arxiv\s*:', content.content, re.I):
                            add('venue', content.content, start, end, 'ieee_post_title_preprint_venue')
                            warnings.append(Diagnostic('preprint_venue_not_journal',
                                'arXiv identifier retained as venue evidence, not assigned to journal.', start, end))
                        else:
                            metadata['journal'] = content.content
                            add('journal', content.content, start, end, 'ieee_post_title_emph_journal')
            # Scanner may first yield a whitespace/comma text segment.
            if gap.strip(' ,\t\r\n') or segment.kind != 'text' or segment.raw.strip(' ,\t\r\n'):
                break
    else:
        warnings.append(Diagnostic('title_style_deferred' if not titles else 'ambiguous_title',
            'Expected one explicit IEEE quoted title; raw retained without title/authors/journal inference.', 0, len(raw)))

    exclusions = protected + [m.span() for m in titles]
    years = []
    for match in _YEAR.finditer(raw):
        start, end = match.span('year')
        if not overlaps(start, end, exclusions):
            value = int(match['year'])
            years.append(value)
            add('year', value, start, end, 'ieee_comma_year_date_or_pages_clause')
    if len(set(years)) == 1:
        metadata['year'] = years[0]
    elif years:
        warnings.append(Diagnostic('ambiguous_year', 'Conflicting explicit year clauses retained; no year selected.', 0, len(raw)))
    return BibMetadataResult(entry, tuple(sorted(metadata.items())), tuple(evidence), tuple(warnings))
