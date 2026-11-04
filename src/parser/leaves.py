"""A3: refine A2 body paragraphs into ordered sentence, formula and figure leaves.

This is a static LaTeX scanner. It does not expand macros or normalize text; B1
can fill ``content`` after this stage without changing ``raw`` or provenance.
"""

from copy import deepcopy
import re

from .gather import SourceSpan, _braced_end, _COMMAND_NAME, _MACRO_DEFINITIONS
from .hierarchy import (HierarchyResult, ParseDiagnostic, ParseNode, _environment_end,
                        _optional_end, _skip_macro_definition, _walk)


_FORMULAS = frozenset({'equation', 'equation*', 'align', 'align*', 'gather',
                       'gather*', 'multline', 'multline*', 'displaymath'})
_FIGURES = frozenset({'figure', 'figure*', 'table', 'table*', 'algorithm', 'algorithm*'})
_LITERALS = frozenset({'verbatim', 'verbatim*', 'lstlisting', 'minted', 'comment'})
_URL = re.compile(r'https?://[^\s{}\\]+')
_ABBREVIATIONS = ('i.e.', 'e.g.', 'et al.', 'fig.', 'figs.', 'eq.', 'eqs.',
                  'dr.', 'mr.', 'mrs.', 'prof.', 'vs.', 'cf.', 'no.')


def _spans(parent: ParseNode, start: int, end: int) -> list[SourceSpan]:
    selected = []
    cursor = 0
    for origin in parent.source_spans:
        right = cursor + origin.end - origin.start
        a, b = max(cursor, start), min(right, end)
        if a < b:
            selected.append(SourceSpan(origin.path, origin.start + a - cursor,
                                       origin.start + b - cursor))
        cursor = right
    return selected


def _command_end(text: str, start: int) -> tuple[str, int]:
    """Skip a command and its static arguments so fake delimiters stay inert."""
    i = start + 1
    match = _COMMAND_NAME.match(text, i)
    if match:
        name, i = match.group(), match.end()
    else:
        name, i = (text[i], i + 1) if i < len(text) else ('', i)
    if name == 'verb':
        if i < len(text) and text[i] == '*': i += 1
        if i < len(text):
            close = text.find(text[i], i + 1)
            i = len(text) if close < 0 else close + 1
        return name, i
    if name in _MACRO_DEFINITIONS:
        return name, _skip_macro_definition(text, i)
    if name not in ('begin', 'end') and i < len(text) and text[i] == '*': i += 1
    # Braced arguments immediately following commands (including whitespace and
    # optional arguments) are protected from sentence and block detection.
    while True:
        arg = i
        while arg < len(text) and text[arg].isspace(): arg += 1
        if arg < len(text) and text[arg] == '[':
            close = _optional_end(text, arg)
        elif arg < len(text) and text[arg] == '{':
            close = _braced_end(text, arg)
        else:
            break
        if close is None: return name, len(text)
        i = close
    return name, i


def _math_end(text: str, start: int, marker: str) -> int | None:
    i = start + len(marker)
    closing = {r'\[': r'\]', r'\(': r'\)'}.get(marker, marker)
    while i < len(text):
        if text.startswith(closing, i) and (i == 0 or text[i - 1] != '\\'):
            return i + len(closing)
        if text[i] == '\\' and closing in ('$', '$$'):
            i += 2
        else:
            i += 1
    return None


def _blocks(text: str):
    """Return block ranges and unsupported regions; skip protected inline text."""
    found = []
    i = 0
    while i < len(text):
        if text[i] == '%':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text[i] == '$' and (i == 0 or text[i - 1] != '\\'):
            marker = '$$' if text.startswith('$$', i) else '$'
            end = _math_end(text, i, marker)
            if end is None: return found, 'unclosed_math'
            if marker == '$$': found.append((i, end, 'block_formula', {'environment': '$$'}))
            i = end
            continue
        if text[i] != '\\':
            i += 1
            continue
        if text.startswith((r'\[', r'\('), i):
            marker = text[i:i + 2]
            end = _math_end(text, i, marker)
            if end is None: return found, 'unclosed_math'
            if marker == r'\[':
                found.append((i, end, 'block_formula', {'environment': r'\['}))
            i = end
            continue
        if text.startswith(r'\begin{', i):
            arg_end = _braced_end(text, i + len(r'\begin'))
            if arg_end is not None:
                env = text[i + len(r'\begin{'):arg_end - 1]
                if env in _FORMULAS | _FIGURES | _LITERALS:
                    end = _environment_end(text, arg_end, env)
                    if end is None: return found, 'unclosed_block'
                    if env in _LITERALS:
                        return found, 'unsupported_literal'
                    kind = 'block_formula' if env in _FORMULAS else 'figure'
                    attrs = {'environment': env}
                    if kind == 'figure':
                        attrs['subtype'] = env.rstrip('*')
                        attrs.update(_figure_attrs(text[i:end]))
                    found.append((i, end, kind, attrs))
                    i = end
                    continue
        _, i = _command_end(text, i)
    return found, None


def _figure_attrs(raw: str) -> dict:
    attrs = {}
    for key in ('caption', 'label'):
        match = re.search(r'\\' + key + r'\*?(?:\[[^\]]*\])?\s*\{', raw)
        if match:
            start = match.end() - 1
            end = _braced_end(raw, start)
            if end is not None: attrs[key] = raw[start + 1:end - 1]
    return attrs


def _sentence_ranges(text: str, start: int, end: int):
    """Conservative punctuation boundaries; retain raw spelling and offsets."""
    ranges = []
    cursor, i = start, start
    while i < end:
        if text[i] == '%':
            newline = text.find('\n', i, end)
            i = end if newline < 0 else newline + 1
            continue
        url = _URL.match(text, i)
        if url:
            i = min(url.end(), end)
            continue
        if text[i] == '$' and (i == 0 or text[i - 1] != '\\'):
            marker = '$$' if text.startswith('$$', i) else '$'
            i = _math_end(text, i, marker) or end
            continue
        if text[i] == '\\':
            if text.startswith(r'\(', i):
                i = _math_end(text, i, r'\(') or end
            else:
                _, i = _command_end(text, i)
            continue
        char = text[i]
        next_char = text[i + 1] if i + 1 < end else ''
        if char in '.?!' and (not next_char or next_char.isspace()):
            prefix = text[max(cursor, i - 16):i + 1].lower()
            abbreviation = char == '.' and any(prefix.endswith(a) for a in _ABBREVIATIONS)
            if not abbreviation:
                a, b = cursor, i + 1
                while a < b and text[a].isspace(): a += 1
                if a < b: ranges.append((a, b))
                cursor = i + 1
        i += 1
    a, b = cursor, end
    while a < b and text[a].isspace(): a += 1
    while b > a and text[b - 1].isspace(): b -= 1
    if a < b: ranges.append((a, b))
    return ranges


def refine_hierarchy(source: HierarchyResult) -> HierarchyResult:
    """Return a deterministic A3 tree; leave A2 input and bibliography unchanged."""
    result = deepcopy(source)
    if result.root is None: return result
    next_id = max(node.occurrence_id for node in _walk(result.root)) + 1

    def make(parent, kind, start, end, attrs=None):
        nonlocal next_id
        node = ParseNode(next_id, kind, parent.raw[start:end], _spans(parent, start, end),
                         attrs=attrs or {})
        next_id += 1
        return node

    def transform(paragraph):
        if paragraph.children: return [paragraph]
        if sum(s.end - s.start for s in paragraph.source_spans) != len(paragraph.raw):
            problem = 'span_mismatch'
            blocks = []
        else:
            blocks, problem = _blocks(paragraph.raw)
        if problem:
            result.status = 'partial'
            result.diagnostics.append(ParseDiagnostic(
                problem, f'A3 retained raw paragraph: {problem}', paragraph.source_spans))
            paragraph.warnings.append(problem)
            return [paragraph]

        def prose(start, end, reuse=False):
            while start < end and paragraph.raw[start].isspace(): start += 1
            while end > start and paragraph.raw[end - 1].isspace(): end -= 1
            if start >= end: return []
            node = paragraph if reuse else make(paragraph, 'paragraph', start, end)
            node.children = [make(paragraph, 'sentence', a, b)
                             for a, b in _sentence_ranges(paragraph.raw, start, end)]
            return [node]

        if not blocks: return prose(0, len(paragraph.raw), reuse=True)
        output, cursor = [], 0
        for start, end, kind, attrs in blocks:
            output.extend(prose(cursor, start))
            output.append(make(paragraph, kind, start, end, attrs))
            cursor = end
        output.extend(prose(cursor, len(paragraph.raw)))
        return output

    def visit(node):
        children = []
        for child in node.children:
            if child.kind == 'paragraph': children.extend(transform(child))
            else:
                visit(child)
                children.append(child)
        node.children = children

    visit(result.root)
    return result
