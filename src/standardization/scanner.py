"""Recognize protected regions without expanding macros or changing source.

This is a bounded-scope lexical scanner, not a TeX compiler. Unclosed constructs
retain the entire remaining input as one malformed segment instead of guessing
where prose starts again. Even malformed input can always be reconstructed.
"""

import re

from .models import Diagnostic, ScanResult, Segment

_COMMAND = re.compile(r'\\([A-Za-z@]+\*?|[^\n])')
_ENVIRONMENT = re.compile(r'\\(begin|end)\{([A-Za-z][A-Za-z0-9*@_-]*)\}')
_BARE_URL = re.compile(r'https?://[^\s<>]+')
_MATH_ENVIRONMENTS = {
    'equation', 'equation*', 'align', 'align*', 'alignat', 'alignat*',
    'gather', 'gather*', 'multline', 'multline*', 'displaymath', 'math',
}
_CODE_ENVIRONMENTS = {'verbatim', 'verbatim*', 'Verbatim', 'lstlisting', 'minted'}
_FORMATTING = {'textbf', 'textit', 'emph', 'textrm', 'textsf', 'texttt', 'underline'}
_CITATIONS = {'cite', 'citep', 'citet', 'citealp', 'citealt', 'parencite',
              'textcite', 'autocite', 'footcite', 'nocite', 'Citep', 'Citet'}


def _verb_end(raw: str, start: int) -> int | None:
    command = _COMMAND.match(raw, start)
    delimiter_at = command.end()
    if delimiter_at >= len(raw) or raw[delimiter_at].isspace():
        return None
    delimiter = raw[delimiter_at]
    newline = raw.find('\n', delimiter_at + 1)
    close = raw.find(delimiter, delimiter_at + 1)
    if close < 0 or (newline >= 0 and newline < close):
        return None
    return close + 1


def _balanced_end(raw: str, start: int, *, comments: bool = True) -> int | None:
    """Read a brace group or optional argument, respecting escapes/comments."""
    stack = ['}' if raw[start] == '{' else ']']
    index = start + 1
    while index < len(raw):
        char = raw[index]
        if char == '\\':
            command = _COMMAND.match(raw, index)
            if command and command[1] in ('verb', 'verb*'):
                index = _verb_end(raw, index)
                if index is None:
                    return None
            else:
                index += 2
            continue
        if char == '%' and comments:
            newline = raw.find('\n', index)
            if newline < 0:
                return None
            index = newline + 1
            continue
        if char == '{':
            stack.append('}')
        elif char == '[' and stack[-1] == ']':
            stack.append(']')
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return None


def _command_end(raw: str, command_end: int, *, required_limit=None, literal_url=False) -> int | None:
    end = command_end
    required = 0
    while end < len(raw):
        if required_limit is not None and required >= required_limit:
            return end
        candidate = end
        while candidate < len(raw) and raw[candidate].isspace():
            candidate += 1
        if candidate >= len(raw) or raw[candidate] not in '{[':
            return end
        if raw[candidate] == '{':
            required += 1
        end = _balanced_end(raw, candidate, comments=not literal_url)
        if end is None:
            return None
    return end


def formatting_argument_span(raw: str) -> tuple[str, int, int] | None:
    """Locate one supported brace argument; offsets refer to the supplied raw.

    Starred/optional/unbraced forms are deliberately not interpreted as the
    ordinary one-argument formatting commands. The caller decides which names
    it actually unwraps (in particular texttt can remain protected).
    """
    command = _COMMAND.match(raw)
    if not command or command[1] not in _FORMATTING:
        return None
    opening = command.end()
    while opening < len(raw) and raw[opening].isspace():
        opening += 1
    if opening >= len(raw) or raw[opening] != '{':
        return None
    closing = _balanced_end(raw, opening)
    if closing != len(raw):
        return None
    return command[1], opening + 1, closing - 1


def _math_end(raw: str, start: int, opener: str, closer: str) -> int | None:
    index = start + len(opener)
    depth = 0
    while index < len(raw):
        if depth == 0 and raw.startswith(closer, index):
            # A double-dollar boundary is not the end of single-dollar math.
            if closer == '$' and raw.startswith('$$', index):
                return None
            return index + len(closer)
        char = raw[index]
        if char == '\\':
            index += 2
            continue
        if char == '%':
            newline = raw.find('\n', index)
            if newline < 0:
                return None
            index = newline + 1
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                return None
        index += 1
    return None


def _environment_end(raw: str, start: int, name: str, opening_end: int) -> int | None:
    if name in _CODE_ENVIRONMENTS:
        close = '\\end{' + name + '}'
        found = raw.find(close, opening_end)
        return None if found < 0 else found + len(close)
    stack = [name]
    index = opening_end
    while index < len(raw):
        if raw[index] == '%':
            newline = raw.find('\n', index)
            if newline < 0:
                return None
            index = newline + 1
            continue
        if raw[index] != '\\':
            index += 1
            continue
        environment = _ENVIRONMENT.match(raw, index)
        if environment:
            action, nested = environment.groups()
            if action == 'begin':
                if nested in _CODE_ENVIRONMENTS:
                    end = _environment_end(raw, index, nested, environment.end())
                    if end is None:
                        return None
                    index = end
                    continue
                stack.append(nested)
            elif nested != stack[-1]:
                return None
            else:
                stack.pop()
                if not stack:
                    return environment.end()
            index = environment.end()
        else:
            command = _COMMAND.match(raw, index)
            if command and command[1] in ('verb', 'verb*'):
                end = _verb_end(raw, index)
                if end is None:
                    return None
                index = end
            else:
                index = command.end() if command else index + 1
    return None


def scan_latex(raw: str) -> ScanResult:
    """Return contiguous top-level segments. Concatenating raw reproduces input."""
    if not isinstance(raw, str):
        raise TypeError('raw must be a string')
    segments = []
    warnings = []
    index = text_start = 0
    while index < len(raw):
        start = index
        kind = None
        end = None
        error = 'unclosed_construct'
        if raw[index] == '%':
            kind = 'comment'
            newline = raw.find('\n', index)
            end = len(raw) if newline < 0 else newline + 1
        elif raw[index] == '$' or raw.startswith(('\\(', '\\['), index):
            opener = ('$$' if raw.startswith('$$', index) else '$') if raw[index] == '$' else raw[index:index + 2]
            closer = {'$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]'}[opener]
            kind = 'inline_math' if opener in ('$', '\\(') else 'block_math'
            end = _math_end(raw, index, opener, closer)
            error = 'unclosed_math'
        elif raw[index] == '{':
            kind = 'group'
            end = _balanced_end(raw, index)
            error = 'unclosed_group'
        elif raw[index] == '}':
            kind, end = 'malformed', index + 1
            warnings.append(Diagnostic('unexpected_closing_brace', 'Unmatched closing brace retained.', index, end))
        elif raw[index] == '\\':
            environment = _ENVIRONMENT.match(raw, index)
            command = _COMMAND.match(raw, index)
            if environment and environment[1] == 'begin':
                name = environment[2]
                kind = 'block_math' if name in _MATH_ENVIRONMENTS else 'environment'
                if name == 'math':
                    kind = 'inline_math'
                if name in _CODE_ENVIRONMENTS:
                    kind = 'verbatim'
                end = _environment_end(raw, index, name, environment.end())
                error = 'unclosed_environment'
            elif command:
                name = command[1]
                base = name.rstrip('*')
                if name in ('verb', 'verb*'):
                    kind, end, error = 'verbatim', _verb_end(raw, index), 'unclosed_verbatim'
                elif not name[0].isalpha() and name[0] != '@':
                    kind, end = 'escape', command.end()
                    if name in (')', ']'):
                        kind = 'malformed'
                        warnings.append(Diagnostic('unexpected_math_closer', 'Math closer without an opener retained.', index, end))
                else:
                    kind = ('citation' if base in _CITATIONS else
                            'url' if base in ('url', 'href', 'path') else
                            'formatting' if base in _FORMATTING else 'command')
                    required_limit = (1 if kind in ('citation', 'formatting') or base in ('url', 'path')
                                      else 2 if base == 'href' else None)
                    end = _command_end(raw, command.end(), required_limit=required_limit,
                                       literal_url=base in ('url', 'path'))
            else:
                kind = 'malformed'
                error = 'dangling_backslash'
        elif raw.startswith(('https://', 'http://'), index):
            kind = 'url'
            end = _BARE_URL.match(raw, index).end()
        if kind is None:
            index += 1
            continue
        if text_start < start:
            segments.append(Segment('text', raw[text_start:start], text_start, start))
        if end is None:
            end = len(raw)
            kind = 'malformed'
            warnings.append(Diagnostic(error, 'Construct could not be closed; remaining source retained.', start, end))
        segments.append(Segment(kind, raw[start:end], start, end))
        index = text_start = end
    if text_start < len(raw):
        segments.append(Segment('text', raw[text_start:], text_start, len(raw)))
    return ScanResult(tuple(segments), tuple(warnings))
