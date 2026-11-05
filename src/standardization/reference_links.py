"""Conservative lexical DOI/HTTP(S) evidence, not resolved reference metadata.

Spans are Unicode character offsets into the exact raw_content argument.
No network, macro expansion, candidate selection, deduplication or mutation.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from .models import Diagnostic
from .scanner import (_COMMAND, _ENVIRONMENT, _CODE_ENVIRONMENTS,
                      _MATH_ENVIRONMENTS, _balanced_end, _command_end,
                      _math_end, _verb_end)
from .bibliography import (_definition_end, _conditional_end,
                           _protected_environment_end, _skip_space_comments)


@dataclass(frozen=True)
class LinkEvidence:
    kind: str
    value: str
    raw: str
    start: int
    end: int
    source_form: str


@dataclass(frozen=True)
class ReferenceLinksResult:
    raw: str
    candidates: tuple[LinkEvidence, ...]
    warnings: tuple[Diagnostic, ...] = ()


# Deliberately a candidate recognizer, not a DOI registration validator.
_DOI = re.compile(r'10\.[0-9]{4,9}/[A-Za-z0-9._;()/:+%\-]+', re.ASCII)
_URL = re.compile(r'https?://[^\s<>"{}\\]+', re.I)
_DOI_HOST = re.compile(r'https?://(?:dx\.)?doi\.org/', re.I)
_FORMAT = {'textbf', 'textit', 'emph', 'textrm', 'textsf', 'texttt', 'underline'}
_DEFINITIONS = {'def', 'gdef', 'edef', 'xdef', 'newcommand', 'renewcommand',
                'providecommand', 'DeclareRobustCommand'}


def normalize_doi_literal(value):
    """Trim a literal DOI or unwrap a plain doi.org URL, without case folding.

    No decoding, punctuation repair, query removal or network validation.
    Original field/evidence spelling must be retained by the caller.
    """
    if not isinstance(value, str):
        raise TypeError('DOI literal must be str')
    value = value.strip()
    if _DOI.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme.lower() in {'http', 'https'} and
            parsed.netloc.lower() in {'doi.org', 'dx.doi.org'} and
            not parsed.query and not parsed.fragment and parsed.path.startswith('/') and
            _DOI.fullmatch(parsed.path[1:])):
        return parsed.path[1:]
    return None


def extract_reference_links(raw_content: str) -> ReferenceLinksResult:
    """Collect all lexical evidence in one bibitem body, preserving occurrence order.

URL targets in url/href accept literal percent signs; href display text is
not scanned. Formatting/group bodies are scanned; unknown command arguments,
comments, math, code, macro definitions and generic environments are deferred.
"""
    if not isinstance(raw_content, str):
        raise TypeError('raw_content must be a string')
    raw = raw_content
    candidates = []
    warnings = []

    def warn(code, start, end, message):
        warnings.append(Diagnostic(code, message, start, end))

    def emit(kind, start, end, form):
        if end > start:
            candidates.append(LinkEvidence(kind, raw[start:end], raw[start:end], start, end, form))

    def trim(start, end):
        original_end = end
        # Bare prose delimiters are ambiguous; preserve their evidence in raw
        # and report the heuristic rather than silently claiming exact metadata.
        while end > start:
            text = raw[start:end]
            if text[-1] in '.,;:!?' or (text[-1] == ')' and text.count(')') > text.count('(')) or (
                    text[-1] == ']' and text.count(']') > text.count('[')):
                end -= 1
            else:
                break
        if end != original_end:
            warn('ambiguous_trailing_punctuation', start, original_end,
                 'Bare-token punctuation trimmed heuristically; inspect original input.')
        return end

    def url(start, end, form):
        target = raw[start:end]
        if not re.match(r'https?://', target, re.I):
            warn('unsupported_link_target', start, end, 'Only literal HTTP(S) targets are recognized.')
            return
        if any(c.isspace() for c in target) or any(c in target for c in '\\{}'):
            warn('nonliteral_link_target', start, end,
                 'Whitespace, macros or LaTeX escapes in target retained without decoding.')
            return
        host_end = len(re.match(r'https?://', target, re.I)[0])
        if host_end == len(target) or target[host_end] in '/?#':
            warn('invalid_link_target', start, end, 'HTTP(S) target has no host.')
            return
        emit('url', start, end, form)
        resolver = _DOI_HOST.match(target)
        if resolver:
            doi_start = start + resolver.end()
            # Query/fragment belongs to the URL, not the DOI path evidence.
            doi_end = min([p for p in (raw.find('?', doi_start, end),
                                      raw.find('#', doi_start, end)) if p >= 0] + [end])
            match = _DOI.fullmatch(raw[doi_start:doi_end])
            if match:
                emit('doi', doi_start, doi_end, 'doi_url')
            else:
                warn('unrecognized_doi_url', start, end,
                     'DOI resolver target retained as URL; path not recognized by heuristic.')

    def walk(start, end, depth=0):
        if depth > 64:
            warn('link_scan_depth_limit', start, end, 'Nested content deferred.')
            return
        i = start
        while i < end:
            c = raw[i]
            if c == '%':
                newline = raw.find('\n', i, end)
                i = end if newline < 0 else newline + 1
                continue
            if c == '$' or raw.startswith(('\\(', '\\['), i):
                op = ('$$' if raw.startswith('$$', i) else '$') if c == '$' else raw[i:i+2]
                close = {'$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]'}[op]
                stop = _math_end(raw, i, op, close)
                if stop is None or stop > end:
                    warn('unclosed_link_scan_math', i, end, 'Malformed math tail deferred.')
                    return
                i = stop
                continue
            if c == '{':
                stop = _balanced_end(raw, i)
                if stop is None or stop > end:
                    warn('unclosed_link_scan_group', i, end, 'Malformed group tail deferred.')
                    return
                walk(i + 1, stop - 1, depth + 1)
                i = stop
                continue
            if c == '\\':
                command = _COMMAND.match(raw, i)
                if not command:
                    i += 1
                    continue
                name = command[1]
                if name == 'newif':
                    target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
                    if target and target[1].startswith('if'):
                        i = target.end()
                        continue
                if name == 'let':
                    target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
                    if target:
                        at = _skip_space_comments(raw, target.end())
                        if at < end and raw[at] == '=':
                            at = _skip_space_comments(raw, at + 1)
                        value = _COMMAND.match(raw, at)
                        if at < end:
                            i = value.end() if value else at + 1
                            continue
                if name.startswith('if') and name != 'ifthenelse':
                    stop = _conditional_end(raw, command.end())
                    warn('link_conditional_deferred', i, end if stop is None else min(stop, end),
                         'Conditional branches were not evaluated; content deferred.')
                    if stop is None or stop > end:
                        return
                    i = stop
                    continue
                if name in ('url', 'href'):
                    opening = command.end()
                    while opening < end and raw[opening].isspace():
                        opening += 1
                    if opening >= end or raw[opening] != '{':
                        warn('malformed_link_command', i, opening, 'Expected braced URL target; tail deferred.')
                        return
                    stop = _balanced_end(raw, opening, comments=False)
                    if stop is None or stop > end:
                        warn('malformed_link_command', i, end, 'Unclosed URL target; tail deferred.')
                        return
                    if name == 'href':
                        display = stop
                        while display < end and raw[display].isspace():
                            display += 1
                        closing = _balanced_end(raw, display) if display < end and raw[display] == '{' else None
                        if closing is None or closing > end:
                            warn('malformed_link_command', i, end, 'Missing/unclosed href display argument; tail deferred.')
                            return
                        after = closing
                    else:
                        after = stop
                    url(opening + 1, stop - 1, name)
                    i = after
                    continue
                if name in ('verb', 'verb*'):
                    stop = _verb_end(raw, i)
                elif name.rstrip('*') in _DEFINITIONS:
                    stop = _definition_end(raw, command)
                else:
                    env = _ENVIRONMENT.match(raw, i)
                    if env and env[1] == 'begin':
                        stop = _protected_environment_end(raw, i, env[2], env.end())
                        if env[2] not in _CODE_ENVIRONMENTS | _MATH_ENVIRONMENTS | {'comment'}:
                            warn('link_environment_deferred', i, end if stop is None else min(stop, end),
                                 'Environment body was not interpreted as bibliography prose.')
                    else:
                        stop = _command_end(raw, command.end(),
                                            required_limit=1 if name in _FORMAT else None)
                        if stop is not None and name in _FORMAT:
                            opening = command.end()
                            while opening < stop and raw[opening].isspace():
                                opening += 1
                            closing = _balanced_end(raw, opening) if opening < stop and raw[opening] == '{' else None
                            if closing == stop:
                                walk(opening + 1, closing - 1, depth + 1)
                            else:
                                warn('link_formatting_deferred', i, min(stop, end),
                                     'Formatting form not supported; arguments retained without scanning.')
                        elif stop is not None and stop > command.end():
                            warn('link_command_arguments_deferred', i, min(stop, end),
                                 'Unknown/citation command arguments were not expanded or scanned.')
                if stop is None or stop > end:
                    warn('unclosed_link_scan_construct', i, end, 'Malformed construct tail deferred.')
                    return
                i = stop
                continue
            match = _URL.match(raw, i, end)
            if match and (i == start or not (raw[i-1].isalnum() or raw[i-1] in '_/')):
                stop = trim(i, match.end())
                url(i, stop, 'bare_url')
                i = match.end()
                continue
            match = _DOI.match(raw, i, end)
            if match and (i == start or not (raw[i-1].isalnum() or raw[i-1] in '_./')):
                stop = trim(i, match.end())
                if _DOI.fullmatch(raw[i:stop]):
                    emit('doi', i, stop, 'bare_doi')
                i = match.end()
                continue
            i += 1

    walk(0, len(raw))
    candidates.sort(key=lambda x: (x.start, x.end, x.kind))
    warnings.sort(key=lambda x: (x.start, x.end, x.code))
    return ReferenceLinksResult(raw, tuple(candidates), tuple(warnings))
