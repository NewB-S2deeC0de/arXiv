"""B1 prose cleanup with protected regions and input-relative diagnostics.

Only allowlisted formatting arguments are unwrapped. No API mutates parser
nodes, splits sentences, expands unknown macros or remaps citation keys.
"""

from dataclasses import dataclass, replace
import re

from .models import Diagnostic, NormalizationResult, Segment
from .scanner import formatting_argument_span, scan_latex
from .bibliography import (_definition_end, _protected_environment_end,
                           _conditional_end, _skip_space_comments)
from .scanner import (_COMMAND, _balanced_end, _command_end, _math_end,
                      _verb_end, _CODE_ENVIRONMENTS, _MATH_ENVIRONMENTS)

_UNWRAP = {'textbf', 'textit', 'emph', 'textrm', 'textsf', 'underline'}
_REFERENCES = {'label', 'ref', 'eqref', 'pageref', 'autoref', 'cref', 'Cref', 'nameref'}
_WHITESPACE = re.compile(r'[ \t\r\n\f\v]+')
_MAX_DEPTH = 64
_TOKEN_SENSITIVE = {'let', 'futurelet', 'newif', 'csname', 'catcode',
                    'afterassignment', 'aftergroup', 'expandafter', 'noexpand',
                    'string', 'meaning', 'the', 'uppercase', 'lowercase'}


@dataclass(frozen=True)
class _Piece:
    """A prose fragment or protected token, still linked to the original raw."""
    kind: str
    raw: str
    value: str
    start: int
    end: int


def _canonical_math(segment):
    raw = segment.raw
    if segment.kind == 'inline_math':
        if raw.startswith(('\\(', r'\begin{math}')):
            body = (raw[2:-2] if raw.startswith('\\(') else
                    raw[len(r'\begin{math}'):-len(r'\end{math}')])
            if not body:
                return raw, Diagnostic('empty_inline_math_deferred',
                    'Empty inline math retained; $$ would be a display delimiter.',
                    segment.start, segment.end)
            candidate = '$' + body + '$'
            scanned = scan_latex(candidate)
            if not scanned.warnings and len(scanned.segments) == 1 and scanned.segments[0].kind == 'inline_math':
                return candidate, None
        if raw.startswith('$'):
            return raw, None
    if segment.kind == 'block_math':
        if raw.startswith(r'\begin{displaymath}'):
            body = raw[len(r'\begin{displaymath}'):-len(r'\end{displaymath}')]
            return r'\begin{equation*}' + body + r'\end{equation*}', None
        if raw.startswith('$$') or raw.startswith('\\['):
            return r'\begin{equation*}' + raw[2:-2] + r'\end{equation*}', None
        if raw.startswith('\\begin{equation}'):
            return raw, None
        # AMS environments carry row alignment and numbering semantics. Keep
        # their TeX environment as the canonical form for that structure.
        if re.match(r'\\begin\{(?:equation\*|(?:align|alignat|gather|multline)\*?)\}', raw):
            return raw, None
    return raw, Diagnostic(
        'math_environment_deferred',
        'Complex/starred math environment retained, including labels and numbering commands.',
        segment.start, segment.end,
    )


def _normalize_math(raw, expected):
    scanned = scan_latex(raw)
    if scanned.warnings:
        return NormalizationResult(raw, scanned.warnings)
    if len(scanned.segments) != 1 or scanned.segments[0].kind != expected:
        return NormalizationResult(raw, (Diagnostic(
            'unexpected_region', f'Expected exactly one {expected} region including delimiters.', 0, len(raw),
        ),))
    content, warning = _canonical_math(scanned.segments[0])
    return NormalizationResult(content, (warning,) if warning else ())


def normalize_inline_math(raw: str) -> NormalizationResult:
    return _normalize_math(raw, 'inline_math')


def normalize_block_math(raw: str) -> NormalizationResult:
    return _normalize_math(raw, 'block_math')


def _collect(raw, offset=0, depth=0):
    """Flatten allowed formatting without trimming arguments at their edges."""
    if depth >= _MAX_DEPTH:
        return [], [Diagnostic('normalization_depth_limit',
            'Deep formatting retained instead of exceeding the recursion budget.',
            offset, offset + len(raw))], True
    scanned = scan_latex(raw)
    warnings = [replace(w, start=w.start + offset, end=w.end + offset)
                for w in scanned.warnings]
    for local in scanned.segments:
        command = re.match(r'\\([A-Za-z@]+\*?)', local.raw) if local.kind == 'command' else None
        if command and (command[1] in _TOKEN_SENSITIVE or
                        command[1].startswith('if') and command[1] != 'ifthenelse'):
            warnings.append(Diagnostic('normalization_token_semantics_deferred',
                'Token-sensitive primitive/conditional retained without prose cleanup.',
                offset + local.start, offset + local.end))
            return [_Piece('command', raw, raw, offset, offset + len(raw))], warnings, True
    pieces = []
    unsafe = False
    for local in scanned.segments:
        segment = Segment(local.kind, local.raw, local.start + offset, local.end + offset)
        if segment.kind == 'formatting':
            argument = formatting_argument_span(segment.raw)
            if argument and argument[0] in _UNWRAP:
                _, start, end = argument
                children, child_warnings, child_unsafe = _collect(
                    segment.raw[start:end], segment.start + start, depth + 1)
                warnings.extend(child_warnings)
                if not child_unsafe:
                    pieces.extend(children)
                    continue
                # Preserve the complete wrapper on malformed/deep input, so
                # another normalization pass cannot expose more source.
                unsafe = True
                warnings.append(Diagnostic('formatting_preserved',
                    'Formatting wrapper retained because its argument cannot be safely normalized.',
                    segment.start, segment.end))
            else:
                warnings.append(Diagnostic('cleanup_deferred',
                    'Code-like texttt or unsupported formatting form retained.',
                    segment.start, segment.end))
        elif segment.kind in ('command', 'group', 'environment', 'comment'):
            command = re.match(r'\\([A-Za-z@]+\*?)', segment.raw)
            if segment.kind != 'command' or not command or command[1] not in _REFERENCES:
                warnings.append(Diagnostic('cleanup_deferred',
                    f'{segment.kind} region retained without cleaning its contents.',
                    segment.start, segment.end))
        elif segment.kind == 'malformed':
            unsafe = True
        value = segment.raw
        if segment.kind in ('inline_math', 'block_math'):
            value, warning = _canonical_math(segment)
            if warning:
                warnings.append(warning)
        pieces.append(_Piece(segment.kind, segment.raw, value, segment.start, segment.end))
    return pieces, warnings, unsafe


def _whitespace(pieces):
    """Merge prose across removed wrappers; never touch protected interiors."""
    merged = []
    for piece in pieces:
        if merged and piece.kind == merged[-1].kind == 'text':
            previous = merged[-1]
            merged[-1] = _Piece('text', previous.raw + piece.raw,
                                previous.value + piece.value, previous.start, piece.end)
        else:
            merged.append(piece)

    def replacement(match):
        whitespace = match[0].replace('\r\n', '\n').replace('\r', '\n')
        return '\n\n' if whitespace.count('\n') >= 2 else ' '

    result = []
    for index, piece in enumerate(merged):
        value = piece.value
        if piece.kind == 'text':
            value = _WHITESPACE.sub(replacement, value)
            if index == 0:
                value = value.lstrip(' \n')
            if index == len(merged) - 1:
                value = value.rstrip(' \n')
        if value:
            result.append(replace(piece, value=value))
    return result


def _protect_math_boundaries(pieces, warnings):
    # Wrapper removal can bring previously separated inline math into contact.
    deferred = set()
    for index in range(1, len(pieces)):
        previous, current = pieces[index - 1:index + 1]
        if (previous.kind == current.kind == 'inline_math'
                and previous.value.endswith('$') and current.value.startswith('$')):
            deferred.update((index - 1, index))
            warnings.append(Diagnostic('adjacent_math_deferred',
                'Adjacent inline math retains original delimiters to avoid a double-dollar boundary.',
                previous.start, current.end))
    return [replace(piece, value=piece.raw) if index in deferred else piece
            for index, piece in enumerate(pieces)]


def _render(pieces):
    content = []
    protected = []
    position = 0
    for piece in pieces:
        content.append(piece.value)
        if piece.kind != 'text':
            protected.append((piece.kind, position, position + len(piece.value), piece.value))
        position += len(piece.value)
    return ''.join(content), protected


def _cleanup_once(raw):
    pieces, warnings, _ = _collect(raw)
    pieces = _protect_math_boundaries(_whitespace(pieces), warnings)
    content, protected = _render(pieces)
    # For example \custom\emph{word} must not become \customword, nor may
    # removing a wrapper turn prose into extra arguments of an unknown macro.
    actual = {(s.kind, s.start, s.end, s.raw) for s in scan_latex(content).segments}
    if any(token not in actual for token in protected):
        warnings.append(Diagnostic('normalization_boundary_deferred',
            'Original node retained because cleanup would change a protected token boundary.',
            0, len(raw)))
        return NormalizationResult(raw, tuple(warnings))
    return NormalizationResult(content, tuple(warnings))


def normalize_text(raw: str) -> NormalizationResult:
    """Unwrap allowed prose formatting, normalize prose whitespace/simple math.

    Raw and parser trees are never modified. Protected regions retain their
    contents. Unstable lexical boundaries cause a conservative whole-node
    fallback, with diagnostics still referring to the original input.
    """
    result = _cleanup_once(raw)
    if result.content != raw and _cleanup_once(result.content).content != result.content:
        return NormalizationResult(raw, result.warnings + (Diagnostic(
            'normalization_boundary_deferred',
            'Original node retained because the candidate cleanup is not idempotent.',
            0, len(raw)),))
    return result


_OUTER = re.compile(r'\\begin\{(figure\*?|table\*?|algorithm\*?)\}')
_LAYOUT = {'figure', 'figure*', 'table', 'table*', 'tabular', 'tabular*', 'tabularx',
           'center', 'flushleft', 'flushright', 'minipage', 'subfigure', 'subtable'}
_DEFINITIONS = {'def', 'gdef', 'edef', 'xdef', 'newcommand', 'renewcommand',
                'providecommand', 'DeclareRobustCommand'}
_TOKEN_PRIMITIVES = {'futurelet', 'csname', 'catcode', 'afterassignment',
                     'aftergroup', 'expandafter', 'noexpand', 'string', 'meaning',
                     'the', 'uppercase', 'lowercase'}


def _scan(raw, *, clean):
    edits, protected, warnings, stack = [], [], [], []
    opening = len(raw) - len(raw.lstrip())
    match = _OUTER.match(raw, opening)
    if not match:
        raise ValueError('Expected one full figure/table/algorithm environment')
    outer = match[1]
    i, closed = opening, False
    while i < len(raw):
        start = i
        if raw[i] == '%':
            line = raw.find('\n', i)
            i = len(raw) if line < 0 else line + 1
            protected.append((start, i, raw[start:i]))
            continue
        if raw[i] == '$' or raw.startswith(('\\(', '\\['), i):
            op = ('$$' if raw.startswith('$$', i) else '$') if raw[i] == '$' else raw[i:i + 2]
            i = _math_end(raw, i, op, {'$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]'}[op])
            if i is None:
                raise ValueError('Unclosed math in figure')
            protected.append((start, i, raw[start:i]))
            continue
        if raw[i] == '{':
            i = _balanced_end(raw, i)
            if i is None:
                raise ValueError('Unclosed group in figure')
            protected.append((start, i, raw[start:i]))
            continue
        if raw[i] == '}':
            raise ValueError('Unmatched brace in figure')
        if raw[i] != '\\':
            i += 1
            continue
        command = _COMMAND.match(raw, i)
        if command is None:
            raise ValueError('Dangling escape in figure')
        name = command[1]
        if name in _TOKEN_PRIMITIVES:
            raise ValueError('Token-sensitive primitive requires an unchanged figure: ' + name)
        if name in ('begin', 'end'):
            arg = command.end()
            while arg < len(raw) and raw[arg].isspace():
                arg += 1
            end = _balanced_end(raw, arg) if arg < len(raw) and raw[arg] == '{' else None
            if end is None:
                raise ValueError('Unclosed environment header')
            env = raw[arg + 1:end - 1]
            if name == 'begin' and env in _CODE_ENVIRONMENTS | {'comment'}:
                i = _protected_environment_end(raw, i, env, end)
                if i is None:
                    raise ValueError('Unclosed literal environment')
                protected.append((start, i, raw[start:i]))
                continue
            if name == 'begin':
                stack.append(env)
                if env not in _LAYOUT | _MATH_ENVIRONMENTS and env not in {'algorithm', 'algorithm*'}:
                    warnings.append(Diagnostic('figure_nested_environment_preserved',
                        'Unknown nested environment retained without internal presentation cleanup.', start, end))
            elif not stack or stack.pop() != env:
                raise ValueError('Mismatched figure environment')
            protected.append((start, end, raw[start:end]))
            i = end
            if start == opening:
                at = i
                while at < len(raw) and raw[at].isspace():
                    at += 1
                if at < len(raw) and raw[at] == '[':
                    option_end = _balanced_end(raw, at)
                    if option_end is None:
                        raise ValueError('Unclosed placement option')
                    option = raw[at + 1:option_end - 1]
                    if clean and outer.rstrip('*') in {'figure', 'table'} and option and all(c in 'htbp!H \t\r\n' for c in option):
                        edits.append((at, option_end))
                    else:
                        protected.append((at, option_end, raw[at:option_end]))
                        if clean:
                            warnings.append(Diagnostic('figure_placement_preserved',
                                'Nonstandard/empty placement option retained.', at, option_end))
                    i = option_end
            if name == 'end' and not stack:
                closed = True
                if raw[i:].strip():
                    raise ValueError('Content outside figure environment')
                break
            continue
        if name == 'newif':
            target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
            if target is None or not target[1].startswith('if'):
                raise ValueError('Malformed conditional declaration')
            end = target.end()
        elif name == 'let':
            target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
            if target is None:
                raise ValueError('Malformed macro alias')
            at = _skip_space_comments(raw, target.end())
            if at < len(raw) and raw[at] == '=':
                at = _skip_space_comments(raw, at + 1)
            value = _COMMAND.match(raw, at)
            if at >= len(raw) or raw[at] in '{}':
                raise ValueError('Malformed macro alias value')
            end = value.end() if value else at + 1
        elif name.startswith('if') and name != 'ifthenelse':
            end = _conditional_end(raw, command.end())
            if end is not None and clean:
                warnings.append(Diagnostic('figure_conditional_preserved',
                    'Conditional retained without evaluating or cleaning its branches.', start, end))
        elif name in ('verb', 'verb*'):
            end = _verb_end(raw, i)
        elif name.rstrip('*') in _DEFINITIONS:
            end = _definition_end(raw, command)
        elif name in ('centering', 'midrule') and clean and all(env in _LAYOUT for env in stack):
            at = command.end()
            while at < len(raw) and raw[at].isspace():
                at += 1
            if at < len(raw) and raw[at] == '[':
                end = _command_end(raw, command.end())
                warnings.append(Diagnostic('figure_presentation_form_preserved',
                    'Presentation command with optional arguments retained.', start, end or len(raw)))
            else:
                edits.append((start, command.end()))
                i = command.end()
                continue
        else:
            end = _command_end(raw, command.end(), literal_url=name in ('url', 'path', 'href'))
        if end is None:
            raise ValueError('Unclosed command argument')
        protected.append((start, end, raw[start:end]))
        i = end
    if not closed or stack:
        raise ValueError('Unclosed figure environment')
    return edits, protected, warnings


def normalize_figure(raw):
    """Return normalized full raw environment; never append attrs.caption/label."""
    if not isinstance(raw, str):
        raise TypeError('raw must be str')
    if re.match(r'\s*\\begin\{algorithm\*?\}', raw):
        return NormalizationResult(raw, (Diagnostic('algorithm_cleanup_deferred',
            'Algorithm subtype retained pending package-specific cleanup rules.', 0, len(raw)),))
    try:
        edits, protected, warnings = _scan(raw, clean=True)
        parts, cursor = [], 0
        for start, end in edits:
            parts.extend((raw[cursor:start], ''))
            cursor = end
        parts.append(raw[cursor:])
        content = ''.join(parts)
        _, after, _ = _scan(content, clean=False)
        after = set(after)
        for start, end, text in protected:
            shift_start = sum(b - a for a, b in edits if b <= start)
            shift_end = sum(b - a for a, b in edits if b <= end)
            if (start - shift_start, end - shift_end, text) not in after:
                raise ValueError('Cleanup changes a protected command boundary')
        return NormalizationResult(content, tuple(warnings))
    except ValueError as exc:
        return NormalizationResult(raw, (Diagnostic('figure_cleanup_deferred', str(exc), 0, len(raw)),))
