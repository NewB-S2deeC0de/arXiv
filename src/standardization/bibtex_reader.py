"""Lossless offline BibTeX reader; literal fields only, no macro execution.

No resource selection, file I/O, inheritance, LaTeX expansion or metadata guessing.
All offsets refer to the original supplied Unicode string.
"""
from dataclasses import dataclass
import re

from .models import SourceContext
from .models import Diagnostic
from .citations import _KEY


@dataclass(frozen=True)
class BibtexField:
    name: str
    raw: str
    expression: str
    value: str | None
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class BibtexEntry:
    entry_type: str
    citation_key: str | None
    citation_key_raw: str
    raw: str
    start: int
    end: int
    fields: tuple[BibtexField, ...]
    source: SourceContext | None
    warnings: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class BibtexFragment:
    raw: str
    start: int
    end: int
    reason: str


@dataclass(frozen=True)
class BibtexReadResult:
    raw: str
    entries: tuple[BibtexEntry, ...]
    unparsed: tuple[BibtexFragment, ...]
    warnings: tuple[Diagnostic, ...]


def _skip(raw, pos, end):
    while pos < end:
        if raw[pos].isspace():
            pos += 1
        elif raw[pos] == '%':
            newline = raw.find('\n', pos, end)
            pos = end if newline < 0 else newline + 1
        else:
            break
    return pos


def _braced_end(raw, pos, limit):
    level = 0
    while pos < limit:
        char = raw[pos]
        if char == '\\':
            pos += 2
            continue
        if char == '{':
            level += 1
        elif char == '}':
            level -= 1
            if level == 0:
                return pos + 1
        pos += 1
    return None


def _quoted_end(raw, pos, limit):
    level = 0
    pos += 1
    while pos < limit:
        char = raw[pos]
        if char == '\\':
            pos += 2
            continue
        if char == '{':
            level += 1
        elif char == '}':
            if not level:
                return None
            level -= 1
        elif char == '"' and not level:
            return pos + 1
        pos += 1
    return None


def _end(raw, opening, limit):
    closing = '}' if raw[opening] == '{' else ')'
    pos = opening + 1
    while pos < limit:
        char = raw[pos]
        if char in '{"':
            pos = (_braced_end if char == '{' else _quoted_end)(raw, pos, limit)
            if pos is None:
                return None
            continue
        if char == '%':
            pos = _skip(raw, pos, limit)
            continue
        if char == closing:
            return pos + 1
        pos += 2 if char == '\\' else 1
    return None


def _parts(raw, start, end):
    """Comma-separated top-level parts, with absolute source offsets."""
    pos, left = start, start
    while pos < end:
        char = raw[pos]
        if char in '{"':
            finish = (_braced_end if char == '{' else _quoted_end)(raw, pos, end)
            if finish is None:
                yield left, end
                return
            pos = finish
        elif char == '%':
            pos = _skip(raw, pos, end)
        elif char == ',':
            yield left, pos
            pos += 1
            left = pos
        else:
            pos += 2 if char == '\\' else 1
    yield left, end


def _literal(raw, start, end):
    """Return literal concatenation; any symbolic atom defers the whole value."""
    values, pos = [], _skip(raw, start, end)
    while pos < end:
        char = raw[pos]
        if char in '{"':
            finish = (_braced_end if char == '{' else _quoted_end)(raw, pos, end)
            if finish is None:
                return None
            values.append(raw[pos + 1:finish - 1])
            pos = finish
        else:
            number = re.match(r'[0-9]+', raw[pos:end])
            if number is None:
                return None
            values.append(number[0])
            pos += len(number[0])
        pos = _skip(raw, pos, end)
        if pos == end:
            return ''.join(values)
        if raw[pos] != '#':
            return None
        pos = _skip(raw, pos + 1, end)
        if pos == end:
            return None
    return None


def read_bibtex(raw, *, source=None):
    """Read caller-supplied .bib text, retaining unsupported directives/values.

    Values preserve TeX spelling. Duplicated fields/keys remain separate records
    with diagnostics; callers must not silently convert fields to a dict.
    """
    if not isinstance(raw, str):
        raise TypeError('raw must be str')
    if source is not None and not isinstance(source, SourceContext):
        raise TypeError('source must be SourceContext or None')
    entries, fragments, warnings = [], [], []
    pos, gap, limit = 0, 0, len(raw)

    def fragment(start, end, reason):
        if end > start:
            fragments.append(BibtexFragment(raw[start:end], start, end, reason))

    while pos < limit:
        if raw[pos] == '%':
            pos = _skip(raw, pos, limit)
            continue
        if raw[pos] != '@':
            pos += 1
            continue
        header = re.match(r'@([A-Za-z][A-Za-z0-9_-]*)\s*([({])', raw[pos:])
        if header is None:
            warnings.append(Diagnostic('bibtex_invalid_header', 'Unrecognized @ header retained.', pos, pos + 1))
            pos += 1
            continue
        opening = pos + header.end() - 1
        end = _end(raw, opening, limit)
        if end is None:
            warnings.append(Diagnostic('bibtex_unclosed_record', 'Unclosed record; remaining raw retained without speculative recovery.', pos, limit))
            break
        kind = header[1].lower()
        fragment(gap, pos, 'outside_entry')
        if kind in {'string', 'preamble', 'comment'}:
            fragment(pos, end, kind)
            if kind != 'comment':
                warnings.append(Diagnostic('bibtex_directive_deferred', '@' + kind + ' retained; no expansion performed.', pos, end))
        else:
            pieces = list(_parts(raw, opening + 1, end - 1))
            key_start, key_end = pieces[0]
            key_start = _skip(raw, key_start, key_end)
            key_raw = raw[key_start:key_end].strip()
            key = key_raw if _KEY.fullmatch(key_raw) and key_raw != '*' else None
            local, fields, names = [], [], set()
            if key is None:
                local.append(Diagnostic('bibtex_invalid_key', 'Invalid key retained without exportable identity.', key_start, key_end))
            if len(pieces) == 1:
                local.append(Diagnostic('bibtex_missing_field_separator', 'Missing comma after entry key.', key_start, key_end))
            for start, finish in pieces[1:]:
                start = _skip(raw, start, finish)
                if start == finish:
                    continue
                field = re.match(r'([A-Za-z][A-Za-z0-9_-]*)\s*=', raw[start:finish])
                if field is None:
                    local.append(Diagnostic('bibtex_malformed_field', 'Field retained in entry raw but not interpreted.', start, finish))
                    continue
                name = field[1].lower()
                value_start = _skip(raw, start + field.end(), finish)
                value_end = finish
                while value_end > value_start and raw[value_end - 1].isspace():
                    value_end -= 1
                value = _literal(raw, value_start, value_end)
                fields.append(BibtexField(name, raw[start:finish], raw[value_start:value_end], value,
                                          start, finish, value_start, value_end))
                if value is None:
                    local.append(Diagnostic('bibtex_value_deferred', 'Symbolic/malformed expression retained; value not evaluated.', value_start, value_end))
                if name in names:
                    local.append(Diagnostic('bibtex_duplicate_field', 'Duplicate field retained; no value selected.', start, finish))
                names.add(name)
                if name in {'crossref', 'xdata'}:
                    local.append(Diagnostic('bibtex_inheritance_deferred', 'Inheritance target retained; fields are not inherited.', start, finish))
            entries.append(BibtexEntry(kind, key, key_raw, raw[pos:end], pos, end,
                                       tuple(fields), source, tuple(local)))
            warnings.extend(local)
        pos, gap = end, end
    fragment(gap, limit, 'outside_or_unparsed')
    seen = set()
    for entry in entries:
        if entry.citation_key is not None and entry.citation_key in seen:
            warnings.append(Diagnostic('bibtex_duplicate_key', 'Repeated entry key retained; no entry chosen.', entry.start, entry.end))
        seen.add(entry.citation_key)
    return BibtexReadResult(raw, tuple(entries), tuple(fragments), tuple(warnings))
