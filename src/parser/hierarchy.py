"""Build an ordered, version-local hierarchy from A1's gathered source."""

from dataclasses import dataclass, field
import re

from .gather import GatheredVersion, SourceSpan, _braced_end, _COMMAND_NAME, _MACRO_DEFINITIONS


@dataclass
class ParseNode:
    occurrence_id: int
    kind: str
    raw: str
    source_spans: list[SourceSpan]
    children: list['ParseNode'] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    content: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BibliographyBlock:
    raw: str
    source_spans: list[SourceSpan]


@dataclass(frozen=True)
class ParseDiagnostic:
    code: str
    message: str
    source_spans: list[SourceSpan]
    severity: str = 'warning'


@dataclass
class HierarchyResult:
    arxiv_id: str
    version: int
    status: str
    root: ParseNode | None
    bibliography: list[BibliographyBlock] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)


_LEVELS = {'chapter': 1, 'section': 2, 'subsection': 3,
           'subsubsection': 4, 'paragraph': 5, 'subparagraph': 6}
_LISTS = frozenset({'itemize', 'enumerate'})
_OPAQUE_ENVS = frozenset({'figure', 'figure*', 'table', 'table*', 'equation',
                          'equation*', 'align', 'align*', 'algorithm', 'algorithm*',
                          'lstlisting', 'minted',
                          'verbatim', 'verbatim*', 'comment'})
_REFERENCES = frozenset({'references', 'bibliography'})


def _environment_end(text, start, environment):
    """Find the matching end command outside comments, verb and command arguments."""
    if environment in ('verbatim', 'verbatim*', 'comment', 'lstlisting', 'minted'):
        closing = f'\\end{{{environment}}}'
        position = text.find(closing, start)
        return None if position < 0 else position + len(closing)
    depth = 1
    i = start
    while i < len(text):
        if text[i] == '%':
            newline = text.find('\n', i)
            i = len(text) if newline < 0 else newline + 1
            continue
        if text[i] != '\\':
            i += 1
            continue
        i += 1
        command = _COMMAND_NAME.match(text, i)
        if command:
            name = command.group()
            i = command.end()
        elif i < len(text):
            name = text[i]
            i += 1
        else:
            break
        if name == 'verb':
            if i < len(text) and text[i] == '*': i += 1
            if i < len(text):
                closing = text.find(text[i], i + 1)
                i = len(text) if closing < 0 else closing + 1
            continue
        arg_at = i
        while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
        end = _braced_end(text, arg_at)
        if end is None: continue
        argument = text[arg_at + 1:end - 1]
        i = end
        if name == 'begin' and argument in ('verbatim', 'verbatim*', 'comment', 'lstlisting', 'minted'):
            literal_end = text.find(f'\\end{{{argument}}}', i)
            if literal_end < 0: return None
            i = literal_end + len(f'\\end{{{argument}}}')
        elif argument == environment and name == 'begin':
            depth += 1
        elif argument == environment and name == 'end':
            depth -= 1
            if depth == 0: return i
    return None


def _tokens(text):
    """Yield structural commands while skipping comments, arguments and opaque environments."""
    i = 0
    while i < len(text):
        if text[i] == '%':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text[i] == '$':
            start = i
            marker = '$$' if text.startswith('$$', i) else '$'
            i += len(marker)
            while i < len(text):
                if text[i] == '\\':
                    i += 2
                elif text.startswith(marker, i):
                    i += len(marker)
                    break
                else:
                    i += 1
            else:
                yield 'unclosed_protected', start, len(text), marker, False
                continue
            yield 'protected', start, i, marker, False
            continue
        if text[i] != '\\':
            i += 1
            continue
        start = i
        i += 1
        match = _COMMAND_NAME.match(text, i)
        if match:
            name = match.group()
            i = match.end()
        elif i < len(text):
            name = text[i]
            i += 1
        else:
            break
        if name in ('[', '('):
            closing = r'\]' if name == '[' else r'\)'
            end = text.find(closing, i)
            i = len(text) if end < 0 else end + len(closing)
            yield ('protected' if end >= 0 else 'unclosed_protected',
                   start, i, f'\\{name}', False)
            continue
        if name == 'verb':
            if i < len(text) and text[i] == '*': i += 1
            if i < len(text):
                end = text.find(text[i], i + 1)
                i = len(text) if end < 0 else end + 1
            continue
        if name in _MACRO_DEFINITIONS:
            if i < len(text) and text[i] == '*': i += 1
            while i < len(text) and text[i].isspace(): i += 1
            macro_end = _braced_end(text, i)
            if macro_end is None and i < len(text) and text[i] == '\\':
                control = _COMMAND_NAME.match(text, i + 1)
                macro_end = control.end() if control else i + 2
            if macro_end is None: continue
            i = macro_end
            for _ in range(2):
                while i < len(text) and text[i].isspace(): i += 1
                if i < len(text) and text[i] == '[':
                    close = text.find(']', i + 1)
                    if close >= 0: i = close + 1
            while i < len(text) and text[i].isspace(): i += 1
            body_end = _braced_end(text, i)
            if body_end is not None: i = body_end
            continue
        if name == 'appendix':
            yield name, start, i, None, False
            continue
        if name == 'printbibliography':
            end = i
            while end < len(text) and text[end].isspace(): end += 1
            if end < len(text) and text[end] == '[':
                close = text.find(']', end + 1)
                if close >= 0: end = close + 1
            i = end
            yield 'bibliography', start, end, None, False
            continue
        if name == 'item':
            end = i
            while end < len(text) and text[end].isspace(): end += 1
            if end < len(text) and text[end] == '[':
                close = text.find(']', end + 1)
                if close >= 0: end = close + 1
            i = end
            yield name, start, i, None, False
            continue
        starred = False
        if name in _LEVELS and i < len(text) and text[i] == '*':
            starred = True
            i += 1
        arg_at = i
        while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
        if arg_at < len(text) and text[arg_at] == '[':
            close = text.find(']', arg_at + 1)
            if close >= 0: arg_at = close + 1
        while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
        end = _braced_end(text, arg_at)
        if end is None: continue
        argument = text[arg_at + 1:end - 1]
        i = end
        if name == 'begin' and argument in _OPAQUE_ENVS:
            close = _environment_end(text, i, argument)
            i = len(text) if close is None else close
            yield ('protected' if close is not None else 'unclosed_protected',
                   start, i, argument, False)
            continue
        if name == 'begin' and argument == 'thebibliography':
            close = _environment_end(text, i, argument)
            i = len(text) if close is None else close
            yield ('bibliography' if close is not None else 'unclosed_protected',
                   start, i, argument, False)
            continue
        if name in ('bibliography', 'addbibresource'):
            yield 'bibliography', start, end, argument, False
            continue
        if name in _LEVELS or name in ('begin', 'end'):
            yield name, start, end, argument, starred
        else:
            # Commands such as \href take more than one braced argument.
            while True:
                next_arg = i
                while next_arg < len(text) and text[next_arg].isspace(): next_arg += 1
                next_end = _braced_end(text, next_arg)
                if next_end is None: break
                i = next_end


def build_hierarchy(gathered: GatheredVersion) -> HierarchyResult:
    """Build raw body nodes; A3 owns fine-grained leaves and B1 owns cleanup."""
    result = HierarchyResult(gathered.arxiv_id, gathered.version, gathered.status, None)
    if gathered.status == 'unparseable': return result

    text = ''.join(segment.text for segment in gathered.segments)
    ranges = []
    cursor = 0
    for segment in gathered.segments:
        end = cursor + len(segment.text)
        ranges.append((cursor, end, segment.origin))
        cursor = end

    def spans(start, end):
        selected = []
        for left, right, origin in ranges:
            if right <= start: continue
            if left >= end: break
            a, b = max(start, left), min(end, right)
            if a < b:
                selected.append(SourceSpan(origin.path, origin.start + a - left,
                                           origin.start + b - left))
        return selected

    sequence = 0

    def node(kind, start, end, attrs=None):
        nonlocal sequence
        sequence += 1
        return ParseNode(sequence, kind, text[start:end], spans(start, end), attrs=attrs or {})

    root = node('document', 0, 0)
    result.root = root
    headings = [root]
    heading_starts = {}
    lists = []  # (list node, owning parent, current item)
    appendix = False
    in_document = False
    cursor = 0

    def parent():
        if lists:
            return lists[-1][2] or lists[-1][0]
        return headings[-1]

    tokens = list(_tokens(text))
    protected = [(start, end) for name, start, end, _, _ in tokens
                 if name in ('protected', 'unclosed_protected')]

    def paragraph(start, end):
        base = start
        for match in re.finditer(r'\n[ \t]*\n', text[base:end]):
            stop = base + match.start()
            boundary_end = base + match.end()
            if any(left < boundary_end and stop < right for left, right in protected):
                continue
            add_paragraph(start, stop)
            start = boundary_end
        add_paragraph(start, end)

    def add_paragraph(start, end):
        while start < end and text[start].isspace(): start += 1
        while end > start and text[end - 1].isspace(): end -= 1
        if start < end: parent().children.append(node('paragraph', start, end))

    for name, start, end, argument, starred in tokens:
        if name == 'unclosed_protected':
            result.status = 'partial'
            result.diagnostics.append(ParseDiagnostic(
                'unclosed_protected', f'Unclosed {argument} environment', spans(start, end)))
            continue
        structural = name in _LEVELS or name in ('begin', 'end', 'item', 'appendix', 'bibliography')
        if not structural: continue
        if name == 'begin' and argument == 'document':
            in_document = True
            cursor = end
            continue
        if name == 'end' and argument == 'document':
            if in_document: paragraph(cursor, start)
            in_document = False
            cursor = end
            continue
        if not in_document: continue
        paragraph(cursor, start)
        cursor = end
        if name in _LEVELS:
            if lists:
                result.status = 'partial'
                result.diagnostics.append(ParseDiagnostic(
                    'heading_in_list', 'Heading appeared before list closed', spans(start, end)))
                lists.clear()
            level = _LEVELS[name]
            while len(headings) > 1 and headings[-1].attrs['level'] >= level:
                headings.pop()
            heading = node('heading', start, end,
                           {'level': level, 'starred': starred,
                            'appendix': appendix, 'title': argument})
            headings[-1].children.append(heading)
            headings.append(heading)
            heading_starts[heading.occurrence_id] = start
        elif name == 'appendix':
            appendix = True
        elif name == 'begin' and argument in _LISTS:
            owner = parent()
            listing = node('list', start, end, {'environment': argument})
            owner.children.append(listing)
            lists.append((listing, owner, None))
        elif name == 'item':
            if not lists:
                result.status = 'partial'
                result.diagnostics.append(ParseDiagnostic(
                    'orphan_item', 'Item outside a supported list', spans(start, end)))
            else:
                item = node('item', start, end)
                lists[-1][0].children.append(item)
                listing, owner, _ = lists[-1]
                lists[-1] = (listing, owner, item)
        elif name == 'end' and argument in _LISTS:
            if not lists or lists[-1][0].attrs['environment'] != argument:
                result.status = 'partial'
                result.diagnostics.append(ParseDiagnostic(
                    'unbalanced_list', 'List end does not match current list', spans(start, end)))
            else:
                lists.pop()
        elif name == 'bibliography':
            bib_start = start
            if (len(headings) > 1 and headings[-1].attrs['title'].strip().lower() in _REFERENCES
                    and all(child.kind == 'paragraph' for child in headings[-1].children)):
                previous = headings.pop()
                headings[-1].children.remove(previous)
                bib_start = heading_starts[previous.occurrence_id]
            result.bibliography.append(BibliographyBlock(text[bib_start:end], spans(bib_start, end)))

    if in_document: paragraph(cursor, len(text))
    if lists:
        result.status = 'partial'
        result.diagnostics.append(ParseDiagnostic('unclosed_list', 'List has no matching end', []))
    for heading in _walk(root):
        if heading.kind == 'heading' and heading.attrs['title'].strip().lower() in _REFERENCES:
            result.status = 'partial'
            result.diagnostics.append(ParseDiagnostic(
                'uncertain_references', 'References heading retained without a certain bibliography boundary',
                heading.source_spans))
    return result


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)
