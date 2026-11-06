"""B3 literal citation recognition and scoped, simultaneous key remapping.

Not a TeX engine: unknown macros/conditionals and protected regions are not
expanded. All spans/edits/diagnostics refer to original raw, never output.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re

from .models import Diagnostic
from .scanner import (_COMMAND, _CODE_ENVIRONMENTS, _MATH_ENVIRONMENTS,
                      _balanced_end, _command_end, _math_end, _verb_end)
from .bibliography import (_definition_end, _conditional_end, _skip_space_comments,
                           _protected_environment_end)


_COMMANDS = frozenset({
    'cite', 'citep', 'citet', 'citealp', 'citealt', 'citeauthor', 'citeyear', 'citeyearpar',
    'Citep', 'Citet', 'Citealp', 'Citealt', 'Citeauthor', 'Citeyear',
    'parencite', 'Parencite', 'textcite', 'Textcite', 'autocite', 'Autocite',
    'footcite', 'footcitetext', 'supercite', 'nocite',
})
_CITE_LOOK = re.compile(r'\\(?:' + '|'.join(sorted(_COMMANDS, key=len, reverse=True)) + r')\*?(?![A-Za-z@])')
_KEY = re.compile(r'[^\s{},\\%$&#^~\x00-\x1f\x7f]+\Z')
_DEFINITIONS = {'def', 'gdef', 'edef', 'xdef', 'newcommand', 'renewcommand',
                'providecommand', 'DeclareRobustCommand'}
_TEXT_COMMANDS = {'textbf', 'textit', 'emph', 'textrm', 'textsf', 'texttt', 'underline',
                  'caption', 'footnote', 'footnotetext', 'text', 'mbox', 'title',
                  'section', 'subsection', 'subsubsection', 'chapter', 'paragraph', 'subparagraph'}
_TEXT_ENVIRONMENTS = {'document', 'abstract', 'center', 'flushleft', 'flushright',
                      'itemize', 'enumerate', 'description', 'quote', 'quotation',
                      'figure', 'figure*', 'table', 'table*', 'tabular', 'tabular*',
                      'tabularx', 'longtable', 'minipage', 'singlespace', 'doublespace'}
_BARE_URL = re.compile(r'https?://[^\s<>]+', re.I)


@dataclass(frozen=True)
class CitationArgument:
    raw: str
    start: int
    end: int


@dataclass(frozen=True)
class CitationKey:
    value: str | None
    raw: str
    start: int
    end: int


@dataclass(frozen=True)
class Citation:
    command: str
    starred: bool
    raw: str
    start: int
    end: int
    optional_arguments: tuple[CitationArgument, ...]
    key_argument: CitationArgument | None
    keys: tuple[CitationKey, ...]
    valid: bool
    wildcard: bool = False


@dataclass(frozen=True)
class CitationScanResult:
    raw: str
    citations: tuple[Citation, ...]
    warnings: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class CitationEdit:
    start: int
    end: int
    old_key: str
    new_key: str


@dataclass(frozen=True)
class CitationKeyDecision:
    start: int
    end: int
    old_key: str
    canonical_key: str | None
    status: str


@dataclass(frozen=True)
class CitationRemapResult:
    arxiv_id: str
    version: int
    raw: str
    content: str
    citations: tuple[Citation, ...]
    decisions: tuple[CitationKeyDecision, ...]
    edits: tuple[CitationEdit, ...]
    warnings: tuple[Diagnostic, ...]


def scan_citations(raw: str) -> CitationScanResult:
    """Recognize allowlisted one-key-list citations, including star/0–2 options."""
    if not isinstance(raw, str):
        raise TypeError('raw must be str')
    citations, warnings = [], []

    def warn(code, message, start, end):
        warnings.append(Diagnostic(code, message, start, end))

    def deferred_if_cite(code, start, end):
        if _CITE_LOOK.search(raw, start, end):
            warn(code, 'Literal citation retained inside a construct whose semantics are deferred.', start, end)

    def parse_citation(command, limit):
        start = command.start()
        name = command[1].rstrip('*')
        options, keys = [], []
        pos = _skip_space_comments(raw, command.end())
        while pos < limit and raw[pos] == '[':
            close = _balanced_end(raw, pos)
            if close is None or close > limit:
                warn('unclosed_citation_option', 'Unclosed optional argument; remaining tail retained.', pos, limit)
                citations.append(Citation(name, command[1].endswith('*'), raw[start:limit], start, limit,
                                          tuple(options), None, (), False))
                return limit
            options.append(CitationArgument(raw[pos:close], pos, close))
            deferred_if_cite('citation_in_option_deferred', pos, close)
            pos = _skip_space_comments(raw, close)
        if pos >= limit or raw[pos] != '{':
            stop = options[-1].end if options else command.end()
            warn('missing_citation_argument', 'Expected a braced citation key list; command retained.', start, stop)
            citations.append(Citation(name, command[1].endswith('*'), raw[start:stop], start, stop,
                                      tuple(options), None, (), False))
            return stop
        close = _balanced_end(raw, pos)
        if close is None or close > limit:
            warn('unclosed_citation_keys', 'Unclosed key argument; remaining tail retained.', pos, limit)
            citations.append(Citation(name, command[1].endswith('*'), raw[start:limit], start, limit,
                                      tuple(options), None, (), False))
            return limit
        valid = len(options) <= 2
        if not valid:
            warn('citation_options_deferred', 'More than two options are outside supported citation syntax.', start, close)
        cursor = pos + 1
        for part in raw[pos + 1:close - 1].split(','):
            left = len(part) - len(part.lstrip())
            right = len(part.rstrip())
            key_start, key_end = cursor + left, cursor + max(left, right)
            literal = raw[key_start:key_end]
            value = literal if _KEY.fullmatch(literal) else None
            keys.append(CitationKey(value, literal, key_start, key_end))
            if value is None:
                valid = False
                warn('invalid_citation_key_list', 'Empty, commented, expanded or whitespace-containing key; citation retained.',
                     cursor, cursor + len(part))
            cursor += len(part) + 1
        wildcard = name == 'nocite' and len(keys) == 1 and keys[0].value == '*'
        if name == 'nocite' and not wildcard and any(k.value == '*' for k in keys):
            valid = False
            warn('mixed_nocite_wildcard', 'Mixed wildcard/key nocite form retained without remapping.', pos, close)
        citations.append(Citation(name, command[1].endswith('*'), raw[start:close], start, close,
            tuple(options), CitationArgument(raw[pos:close], pos, close), tuple(keys), valid, wildcard))
        return close

    def walk(start, limit, depth=0):
        if depth > 64:
            warn('citation_depth_limit', 'Nested content retained at scanner depth limit.', start, limit)
            return
        i = start
        while i < limit:
            if raw[i] == '%':
                newline = raw.find('\n', i, limit)
                i = limit if newline < 0 else newline + 1
                continue
            url = _BARE_URL.match(raw, i, limit)
            if url:
                i = url.end()
                continue
            if raw[i] == '$' or raw.startswith(('\\(', '\\['), i):
                op = ('$$' if raw.startswith('$$', i) else '$') if raw[i] == '$' else raw[i:i+2]
                stop = _math_end(raw, i, op, {'$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]'}[op])
                if stop is None or stop > limit:
                    warn('unclosed_citation_scan_math', 'Malformed math tail retained.', i, limit)
                    return
                deferred_if_cite('citation_in_math_deferred', i, stop)
                i = stop
                continue
            if raw[i] == '{':
                stop = _balanced_end(raw, i)
                if stop is None or stop > limit:
                    warn('unclosed_citation_scan_group', 'Malformed group tail retained.', i, limit)
                    return
                walk(i + 1, stop - 1, depth + 1)
                i = stop
                continue
            if raw[i] != '\\':
                i += 1
                continue
            command = _COMMAND.match(raw, i)
            if not command:
                warn('dangling_citation_scan_escape', 'Dangling escape retained.', i, limit)
                return
            name, base = command[1], command[1].rstrip('*')
            if base in _COMMANDS:
                i = parse_citation(command, limit)
                continue
            if not name[0].isalpha() and name[0] != '@':
                i = command.end()
                continue
            if name == 'newif':
                target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
                if target and target[1].startswith('if'):
                    i = target.end()
                    continue
            if name == 'let':
                target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
                if target:
                    at = _skip_space_comments(raw, target.end())
                    if at < limit and raw[at] == '=':
                        at = _skip_space_comments(raw, at + 1)
                    value = _COMMAND.match(raw, at)
                    if at < limit:
                        stop = value.end() if value else at + 1
                        deferred_if_cite('citation_alias_deferred', i, stop)
                        i = stop
                        continue
            if base in _DEFINITIONS:
                stop = _definition_end(raw, command)
                if stop is not None:
                    deferred_if_cite('citation_macro_definition_deferred', i, min(stop, limit))
            elif name.startswith('if') and name != 'ifthenelse':
                stop = _conditional_end(raw, command.end())
                warn('citation_conditional_deferred', 'Conditional branches not evaluated.', i,
                     limit if stop is None else min(stop, limit))
            elif name in ('verb', 'verb*'):
                stop = _verb_end(raw, i)
            elif name in ('begin', 'end'):
                opening = _skip_space_comments(raw, command.end())
                close = _balanced_end(raw, opening) if opening < limit and raw[opening] == '{' else None
                if close is None or close > limit:
                    warn('malformed_citation_environment', 'Malformed environment header; tail retained.', i, limit)
                    return
                env = raw[opening + 1:close - 1]
                if name == 'end' or env in _TEXT_ENVIRONMENTS:
                    i = close
                    continue
                stop = _protected_environment_end(raw, i, env, close)
                if stop is not None and env not in _CODE_ENVIRONMENTS | {'comment'}:
                    deferred_if_cite('citation_in_math_deferred' if env in _MATH_ENVIRONMENTS
                                     else 'citation_environment_deferred', i, min(stop, limit))
            elif name in ('url', 'path', 'href'):
                opening = _skip_space_comments(raw, command.end())
                close = _balanced_end(raw, opening, comments=False) if opening < limit and raw[opening] == '{' else None
                if close is None or close > limit:
                    warn('malformed_citation_scan_url', 'Malformed URL command; tail retained.', i, limit)
                    return
                stop = close
                if name == 'href':
                    display = _skip_space_comments(raw, close)
                    stop = _balanced_end(raw, display) if display < limit and raw[display] == '{' else None
                    if stop is not None and stop <= limit:
                        walk(display + 1, stop - 1, depth + 1)
            elif base in _TEXT_COMMANDS:
                pos = _skip_space_comments(raw, command.end())
                while pos < limit and raw[pos] == '[':
                    close = _balanced_end(raw, pos)
                    if close is None or close > limit:
                        warn('unclosed_citation_text_option', 'Malformed text wrapper; tail retained.', i, limit)
                        return
                    walk(pos + 1, close - 1, depth + 1)
                    pos = _skip_space_comments(raw, close)
                stop = _balanced_end(raw, pos) if pos < limit and raw[pos] == '{' else None
                if stop is not None and stop <= limit:
                    walk(pos + 1, stop - 1, depth + 1)
                elif pos < limit and raw[pos] != '{':
                    stop = command.end()
                    warn('citation_text_wrapper_deferred', 'Unbraced text wrapper retained.', i, stop)
            else:
                stop = _command_end(raw, command.end())
                if stop is not None:
                    if 'cite' in base.lower():
                        warn('unsupported_citation_command', 'Citation-like macro not in the explicit allowlist.', i, min(stop, limit))
                    else:
                        deferred_if_cite('citation_command_arguments_deferred', i, min(stop, limit))
            if stop is None or stop > limit:
                warn('unclosed_citation_scan_construct', 'Unclosed protected construct; remaining tail retained.', i, limit)
                return
            i = stop

    walk(0, len(raw))
    citations.sort(key=lambda c: c.start)
    warnings.sort(key=lambda w: (w.start, w.end, w.code))
    return CitationScanResult(raw, tuple(citations), tuple(warnings))


def remap_citations(raw, mapping, *, arxiv_id, version):
    """Use {(publication, version, old_key): canonical_key | None} for this raw.

    None marks a known upstream ambiguity. Mappings must point directly to
    final keys: chains/cycles in the same publication/version are rejected.
    No dedup, canonical key generation, alias expansion or tree mutation.
    """
    if not isinstance(arxiv_id, str) or not arxiv_id or type(version) is not int or version < 1:
        raise ValueError('Explicit publication id and positive integer version are required')
    if not isinstance(mapping, Mapping):
        raise TypeError('mapping must be a scoped Mapping, not a global old_key dictionary')
    mapping = dict(mapping)
    for scope, target in mapping.items():
        if (not isinstance(scope, tuple) or len(scope) != 3 or not isinstance(scope[0], str)
                or not scope[0] or type(scope[1]) is not int or scope[1] < 1
                or not isinstance(scope[2], str) or not _KEY.fullmatch(scope[2]) or scope[2] == '*'):
            raise ValueError('Mapping keys must be (arxiv_id, version, literal_old_key)')
        if target is not None and (not isinstance(target, str) or not _KEY.fullmatch(target) or target == '*'):
            raise ValueError('Canonical keys must be nonempty literal keys without TeX injection/wildcards')
    for (pub, ver, old), target in mapping.items():
        if target is not None and (pub, ver, target) in mapping and mapping[pub, ver, target] != target:
            raise ValueError('Mapping contains a chain/cycle or points to an ambiguous key; provide final canonical keys')
    scanned = scan_citations(raw)
    warnings, decisions, edits = list(scanned.warnings), [], []
    for citation in scanned.citations:
        if not citation.valid:
            continue
        for key in citation.keys:
            if citation.wildcard:
                decisions.append(CitationKeyDecision(key.start, key.end, key.value, None, 'wildcard'))
                continue
            scope = (arxiv_id, version, key.value)
            if scope not in mapping:
                status, canonical = 'unresolved', None
                warnings.append(Diagnostic('unknown_citation_key', 'No mapping in this publication/version; original key retained.', key.start, key.end))
            elif mapping[scope] is None:
                status, canonical = 'ambiguous', None
                warnings.append(Diagnostic('ambiguous_citation_mapping', 'Upstream key conflict; original key retained.', key.start, key.end))
            else:
                canonical = mapping[scope]
                status = 'unchanged' if canonical == key.value else 'remapped'
                if status == 'remapped':
                    edits.append(CitationEdit(key.start, key.end, key.value, canonical))
            decisions.append(CitationKeyDecision(key.start, key.end, key.value, canonical, status))
    edits.sort(key=lambda e: e.start)
    parts, cursor = [], 0
    for edit in edits:
        parts.extend((raw[cursor:edit.start], edit.new_key))
        cursor = edit.end
    parts.append(raw[cursor:])
    warnings.sort(key=lambda w: (w.start, w.end, w.code))
    return CitationRemapResult(arxiv_id, version, raw, ''.join(parts), scanned.citations,
                              tuple(decisions), tuple(edits), tuple(warnings))
