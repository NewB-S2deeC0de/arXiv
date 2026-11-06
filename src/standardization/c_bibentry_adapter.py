"""Project bibitem and BibTeX evidence into C BibEntry payloads.

Keeps evidence/unknowns/version outside the lossy final C row. This module
does not require pyarrow or choose a reference's canonical citation key.
"""
from dataclasses import dataclass
from collections.abc import Mapping
import re
from urllib.parse import urlsplit

from .bibtex_reader import BibtexEntry, _braced_end
from .models import BibItem, Diagnostic
from .reference_links import LinkEvidence, extract_reference_links, normalize_doi_literal
from .reference_links import normalize_doi_literal as _doi_literal


@dataclass(frozen=True)
class CBibEntryPayload:
    arxiv_id: str
    citation_key: str
    entry_type: str
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    doi: str | None
    journal: str | None
    url: str | None

    def as_kwargs(self):
        return dict(arxiv_id=self.arxiv_id, citation_key=self.citation_key,
                    entry_type=self.entry_type, title=self.title, authors=list(self.authors),
                    year=self.year, doi=self.doi, journal=self.journal, url=self.url)


@dataclass(frozen=True)
class PreparedCBibEntry:
    entry: BibItem
    payload: CBibEntryPayload | None
    unknown_fields: tuple[str, ...]
    link_evidence: tuple[LinkEvidence, ...]
    warnings: tuple[Diagnostic, ...]
    provided_metadata: tuple[tuple[str, object], ...]
    offset_basis: str = 'entry_raw_content'


_FIELDS = ('entry_type', 'title', 'authors', 'year', 'doi', 'journal', 'url')


def prepare_c_bibentry(entry, *, arxiv_id=None, metadata=None):
    """Make a C-compatible payload with conservative DOI/URL evidence selection.

    Optional metadata must already be parsed/approved by the caller; this
    adapter does not infer title/authors/year/type from raw prose. Missing
    authors -> [] and type -> misc follow C defaults, with unknowns retained
    in the staging wrapper. Ambiguous/conflicting links remain None.
    """
    if not isinstance(entry, BibItem):
        raise TypeError('entry must be a B2 BibItem')
    supplied_id = entry.source.arxiv_id if entry.source else None
    arxiv_id = supplied_id if arxiv_id is None else arxiv_id
    if not isinstance(arxiv_id, str) or not arxiv_id:
        raise ValueError('A publication id is required; do not infer it from reference text')
    if supplied_id is not None and supplied_id != arxiv_id:
        raise ValueError('Publication id disagrees with B2 provenance')
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise TypeError('metadata must be a mapping or None')
    if set(metadata) - set(_FIELDS):
        raise ValueError('Unsupported metadata fields: ' + ', '.join(sorted(set(metadata) - set(_FIELDS))))
    clean = {}
    for field, value in metadata.items():
        if field == 'authors':
            if value is not None and (not isinstance(value, (list, tuple)) or
                                      any(not isinstance(x, str) or not x.strip() for x in value)):
                raise TypeError('authors must be a list/tuple of nonempty strings or None')
            clean[field] = None if value is None else tuple(value)
        elif field == 'year':
            if value is not None and type(value) is not int:
                raise TypeError('year must be int or None, not bool/string')
            clean[field] = value
        else:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise TypeError(field + ' must be a nonempty string or None')
            clean[field] = value
    links = extract_reference_links(entry.raw_content)
    warnings = list(links.warnings)
    unknown = []

    def warning(code, message):
        warnings.append(Diagnostic(code, message, 0, len(entry.raw_content)))

    for field in ('doi', 'url'):
        all_candidates = [c for c in links.candidates if c.kind == field]
        # Do not promote punctuation-trimmed or other warned evidence silently.
        usable = [c for c in all_candidates if not any(
            w.start < c.end and c.start < w.end for w in links.warnings)]
        values = {c.value for c in usable}
        supplied = clean.get(field)
        invalid_supplied = False
        if field == 'doi' and supplied is not None:
            normalized = normalize_doi_literal(supplied)
            invalid_supplied = normalized is None
            if invalid_supplied:
                warning('unsupported_provided_doi', 'Provided DOI retained in evidence but is not a supported literal.')
            supplied = normalized
        if supplied is not None:
            values.add(supplied)
        if len(values) == 1 and len(usable) == len(all_candidates) and not invalid_supplied:
            clean[field] = next(iter(values))
        elif invalid_supplied or len(values) > 1 or all_candidates and len(usable) != len(all_candidates):
            clean[field] = None
            warning('ambiguous_' + field, 'Multiple/conflicting or warned evidence retained; no final ' + field + ' selected.')
        else:
            clean[field] = supplied
    for field in _FIELDS:
        if clean.get(field) is None:
            unknown.append(field)
    if clean.get('entry_type') is None:
        warning('c_entry_type_fallback', 'C requires a default type; misc is a fallback, not an inferred reference type.')
    if clean.get('authors') is None:
        warning('c_authors_unknown', 'C authors=[] is a projection; author metadata remains unknown in this wrapper.')
    payload = None
    if entry.citation_key is None:
        warning('c_record_missing_key', 'Malformed/missing original key retained in staging; no C row constructed.')
    else:
        payload = CBibEntryPayload(arxiv_id, entry.citation_key, clean.get('entry_type') or 'misc',
            clean.get('title'), clean.get('authors') or (), clean.get('year'),
            clean.get('doi'), clean.get('journal'), clean.get('url'))
    # Store a snapshot of caller fields, not the values after link selection.
    provided = tuple((field, tuple(metadata[field]) if field == 'authors' and metadata[field] is not None
                      else metadata[field]) for field in _FIELDS if field in metadata)
    return PreparedCBibEntry(entry, payload, tuple(unknown), links.candidates,
                             tuple(warnings), provided)


def to_c_bibentry(prepared, *, entry_factory):
    """Construct actual parser.contract.BibEntry via an explicitly supplied class."""
    if not isinstance(prepared, PreparedCBibEntry):
        raise TypeError('prepared must be PreparedCBibEntry')
    if prepared.payload is None:
        raise ValueError('No exportable key; keep this occurrence in staging/diagnostics')
    return entry_factory(**prepared.payload.as_kwargs())


@dataclass(frozen=True)
class PreparedBibtexEntry:
    entry: BibtexEntry
    payload: CBibEntryPayload | None
    unknown_fields: tuple[str, ...]
    warnings: tuple[Diagnostic, ...]
    offset_basis: str = 'bib_file_raw'


def _authors(value):
    names, left, pos = [], 0, 0
    while pos < len(value):
        if value[pos] == '{':
            end = _braced_end(value, pos, len(value))
            if end is None:
                return None
            pos = end
            continue
        if value[pos] == '\\':
            pos += 2
            continue
        match = re.match(r'\s+and\s+', value[pos:], re.I)
        if match:
            names.append(value[left:pos].strip())
            pos += len(match[0]);left = pos
        else:
            pos += 1
    names.append(value[left:].strip())
    if any(not name or name.lower() == 'others' for name in names):
        return None
    return tuple(names)


def prepare_bibtex_entry(entry, *, arxiv_id=None):
    """Project supported literal fields; duplicates and symbolic values unknown.

    Author names retain BibTeX spelling/order (including corporate braces).
    Nulls/default authors=[] do not erase unknown/conflict evidence in wrapper.
    """
    if not isinstance(entry, BibtexEntry):
        raise TypeError('entry must be BibtexEntry')
    source_id = entry.source.arxiv_id if entry.source is not None else None
    arxiv_id = source_id if arxiv_id is None else arxiv_id
    if not isinstance(arxiv_id, str) or not arxiv_id.strip():
        raise ValueError('Explicit publication identity required')
    if source_id is not None and arxiv_id != source_id:
        raise ValueError('Publication disagrees with BibTeX provenance')
    warnings = list(entry.warnings)

    def warning(code, field, message):
        warnings.append(Diagnostic(code, message, field.start, field.end))

    def literal(name):
        fields = [f for f in entry.fields if f.name == name]
        if len(fields) != 1:
            if fields:
                warning('bibtex_projection_ambiguous_field', fields[0],
                        'Repeated '+name+' retained; no value selected.')
            return None, None
        f = fields[0]
        return (f.value if f.value is not None and f.value.strip() else None), f

    values = {'entry_type':entry.entry_type}
    for target, source in [('title','title'),('journal','journal'),('doi','doi'),('url','url'),
                            ('year','year'),('authors','author')]:
        value, evidence = literal(source)
        if value is not None and target == 'year':
            if re.fullmatch(r'[0-9]{4}', value.strip()):
                value = int(value.strip())
            else:
                warning('bibtex_year_deferred', evidence, 'Year is not a literal four-digit year.')
                value = None
        elif value is not None and target == 'authors':
            value = _authors(value)
            if value is None:
                warning('bibtex_authors_deferred', evidence, 'Incomplete/unsupported author list retained.')
        elif value is not None and target == 'doi':
            value = _doi_literal(value)
            if value is None:
                warning('bibtex_doi_deferred', evidence, 'DOI field outside supported literal grammar retained.')
                value = None
        elif value is not None and target == 'url':
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme.lower() in {'http','https'} and bool(parsed.netloc) and not re.search(r'\s',value)
            except ValueError:
                valid = False
            if not valid:
                warning('bibtex_url_deferred', evidence, 'URL field outside supported HTTP(S) grammar retained.')
                value = None
        values[target] = value
    unknown = tuple(name for name,value in values.items() if value is None)
    payload = None if entry.citation_key is None else CBibEntryPayload(
        arxiv_id,entry.citation_key,values['entry_type'],values['title'],values['authors'] or (),
        values['year'],values['doi'],values['journal'],values['url'])
    return PreparedBibtexEntry(entry,payload,unknown,tuple(warnings))
