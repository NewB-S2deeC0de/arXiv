"""B2 stage 1: extract literal thebibliography/bibitem without changing raw.

Comments, math, code and macro arguments are protected. Unknown macros are not
expanded. Bibliographies hidden inside brace groups/conditionals are deferred.
Missing or ambiguous boundaries never produce an automatic exclusion span.
"""

from dataclasses import dataclass, field
import re

from .models import (   
    BibItem, BibliographyRegion, BibliographyResult, RawFragment, SourceContext,
)
from .models import Diagnostic
from .scanner import (
    _COMMAND, _CODE_ENVIRONMENTS, _MATH_ENVIRONMENTS, _balanced_end,
    _command_end, _environment_end, _math_end, _verb_end,
)
from pathlib import PurePosixPath
from .source_mapping import MappedSourceSpan, RawSourceMap

_KEY = re.compile(r'[^\s{},\\%]+\Z')
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9*@_-]*\Z')
_BIB_MARKER = re.compile(r'\\begin\s*\{thebibliography\}')
_EXTERNAL = {'bibliography', 'addbibresource', 'printbibliography'}
_DEFINITIONS = {'def', 'gdef', 'edef', 'xdef', 'newcommand', 'renewcommand',
                'providecommand', 'DeclareRobustCommand'}


def _protected_environment_end(raw, start, name, opening_end):
    if name == 'comment':
        marker = '\\end{comment}'
        position = raw.find(marker, opening_end)
        return None if position < 0 else position + len(marker)
    return _environment_end(raw, start, name, opening_end)


def _conditional_end(raw, opening_end):
    """Skip unresolved primitive/custom if...fi branches, never evaluate them."""
    depth = 1
    index = opening_end
    while index < len(raw):
        if raw[index] == '%':
            newline = raw.find('\n', index)
            index = len(raw) if newline < 0 else newline + 1
            continue
        if raw[index] == '{':
            end = _balanced_end(raw, index)
            if end is None:
                return None
            index = end
            continue
        if raw[index] != '\\':
            index += 1
            continue
        command = _COMMAND.match(raw, index)
        if command is None:
            return None
        if command[1] in ('verb', 'verb*'):
            end = _verb_end(raw, index)
            if end is None:
                return None
            index = end
            continue
        if command[1] == 'begin':
            opening = _skip_space_comments(raw, command.end())
            closing = _balanced_end(raw, opening) if opening < len(raw) and raw[opening] == '{' else None
            if closing is not None:
                name = raw[opening + 1:closing - 1]
                if name in _CODE_ENVIRONMENTS | _MATH_ENVIRONMENTS | {'comment'}:
                    end = _protected_environment_end(raw, index, name, closing)
                    if end is None:
                        return None
                    index = end
                    continue
        if command[1].startswith('if') and command[1] != 'ifthenelse':
            depth += 1
        elif command[1] == 'fi':
            depth -= 1
            if depth == 0:
                return command.end()
        index = command.end()
    return None


@dataclass(frozen=True)
class _Header:
    key: str | None
    key_raw: str | None
    label: str | None
    content_start: int
    warnings: tuple[Diagnostic, ...]
    valid: bool


@dataclass(frozen=True)
class _Event:
    kind: str
    name: str
    start: int
    end: int
    header: _Header | None = None


@dataclass
class _Draft:
    start: int
    body_start: int
    header_valid: bool
    stack: list[str] = field(default_factory=lambda: ['thebibliography'])
    items: list[_Event] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    ambiguous: bool = False
    nested: bool = False


def _skip_space_comments(raw, index):
    while index < len(raw):
        if raw[index].isspace():
            index += 1
        elif raw[index] == '%':
            newline = raw.find('\n', index)
            if newline < 0:
                return len(raw)
            index = newline + 1
        else:
            break
    return index


def _item_header(raw, start, command_end):
    index = _skip_space_comments(raw, command_end)
    label = None
    warnings = []
    last_end = command_end
    if index < len(raw) and raw[index] == '[':
        closing = _balanced_end(raw, index)
        if closing is None:
            warning = Diagnostic('unclosed_bibitem_label',
                'Optional label is unclosed; remaining raw retained.', index, len(raw))
            return _Header(None, None, None, len(raw), (warning,), False)
        label = raw[index + 1:closing - 1]
        last_end = closing
        index = _skip_space_comments(raw, closing)
    if index >= len(raw) or raw[index] != '{':
        warning = Diagnostic('missing_citation_key',
            'Bibitem requires a braced citation key; content retained.', start, last_end)
        return _Header(None, None, label, last_end, (warning,), False)
    closing = _balanced_end(raw, index)
    if closing is None:
        warning = Diagnostic('unclosed_citation_key',
            'Citation key is unclosed; remaining raw retained.', index, len(raw))
        return _Header(None, raw[index + 1:], label, len(raw), (warning,), False)
    key_raw = raw[index + 1:closing - 1]
    key = key_raw if _KEY.fullmatch(key_raw) else None
    if key is None:
        warnings.append(Diagnostic('invalid_citation_key',
            'Empty, expanded or whitespace-containing key retained without guessing a replacement.',
            index, closing))
    return _Header(key, key_raw, label, closing, tuple(warnings), key is not None)


def _definition_end(raw, command):
    """Skip literal macro declarations, including an unbraced control-word name."""
    index = _skip_space_comments(raw, command.end())
    if index >= len(raw):
        return None
    if raw[index] == '{':
        target_end = _balanced_end(raw, index)
    else:
        target = _COMMAND.match(raw, index)
        target_end = target.end() if target else None
    if target_end is None:
        return None
    if command[1].rstrip('*') not in {'def', 'gdef', 'edef', 'xdef'}:
        return _command_end(raw, target_end)
    index = target_end
    while index < len(raw):
        if raw[index] == '%':
            newline = raw.find('\n', index)
            if newline < 0:
                return None
            index = newline + 1
        elif raw[index] == '{':
            return _balanced_end(raw, index)
        elif raw[index] == '\\':
            token = _COMMAND.match(raw, index)
            index = token.end() if token else index + 1
        else:
            index += 1
    return None


def _events(raw):
    """Literal structural events outside protected arguments, math and code."""
    index = 0
    while index < len(raw):
        if raw[index] == '%':
            newline = raw.find('\n', index)
            index = len(raw) if newline < 0 else newline + 1
            continue
        if raw[index] == '$' or raw.startswith(('\\(', '\\['), index):
            opener = ('$$' if raw.startswith('$$', index) else '$') if raw[index] == '$' else raw[index:index + 2]
            closer = {'$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]'}[opener]
            end = _math_end(raw, index, opener, closer)
            if end is None:
                yield _Event('issue', 'unclosed_math', index, len(raw))
                return
            index = end
            continue
        if raw[index] == '{':
            end = _balanced_end(raw, index)
            if end is None:
                yield _Event('issue', 'unclosed_group', index, len(raw))
                return
            if _BIB_MARKER.search(raw[index:end]):
                yield _Event('issue', 'grouped_bibliography_deferred', index, end)
            index = end
            continue
        if raw[index] == '}':
            yield _Event('issue', 'unexpected_closing_brace', index, index + 1)
            index += 1
            continue
        if raw[index] != '\\':
            index += 1
            continue
        command = _COMMAND.match(raw, index)
        if command is None:
            yield _Event('issue', 'dangling_backslash', index, len(raw))
            return
        name = command[1]
        if name == 'newif':
            target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
            if target and target[1].startswith('if'):
                index = target.end()
                continue
        if name == 'let':
            target = _COMMAND.match(raw, _skip_space_comments(raw, command.end()))
            if target:
                value_start = _skip_space_comments(raw, target.end())
                if value_start < len(raw) and raw[value_start] == '=':
                    value_start = _skip_space_comments(raw, value_start + 1)
                value = _COMMAND.match(raw, value_start)
                if value_start < len(raw):
                    index = value.end() if value else value_start + 1
                    continue
        if name.rstrip('*') in _DEFINITIONS:
            end = _definition_end(raw, command)
            if end is None:
                yield _Event('issue', 'malformed_macro_definition', index, len(raw))
                return
            if _BIB_MARKER.search(raw[command.end():end]):
                yield _Event('issue', 'macro_bibliography_deferred', index, end)
            index = end
            continue
        if name.startswith('if') and name != 'ifthenelse':
            end = _conditional_end(raw, command.end())
            yield _Event('issue', 'conditional_content_deferred', index, end or len(raw))
            if end is None:
                return
            index = end
            continue
        if name in ('verb', 'verb*'):
            end = _verb_end(raw, index)
            if end is None:
                yield _Event('issue', 'unclosed_verbatim', index, len(raw))
                return
            index = end
            continue
        if not name[0].isalpha() and name[0] != '@':
            index = command.end()
            continue
        if name == 'bibitem':
            header = _item_header(raw, index, command.end())
            yield _Event('bibitem', name, index, header.content_start, header)
            index = header.content_start
            continue
        if name in ('begin', 'end'):
            opening = _skip_space_comments(raw, command.end())
            closing = _balanced_end(raw, opening) if opening < len(raw) and raw[opening] == '{' else None
            if closing is not None and _NAME.fullmatch(raw[opening + 1:closing - 1]):
                environment = raw[opening + 1:closing - 1]
                if name == 'begin' and environment in _CODE_ENVIRONMENTS | {'comment'}:
                    end = _protected_environment_end(raw, index, environment, closing)
                    if end is None:
                        yield _Event('issue', 'unclosed_code_environment', index, len(raw))
                        return
                    index = end
                    continue
                if name == 'begin' and environment in _MATH_ENVIRONMENTS:
                    end = _environment_end(raw, index, environment, closing)
                    if end is None:
                        yield _Event('issue', 'unclosed_math_environment', index, len(raw))
                        return
                    index = end
                    continue
                yield _Event(name, environment, index, closing)
                index = closing
                continue
        end = _command_end(raw, command.end(), literal_url=name in ('url', 'path'))
        if end is None:
            yield _Event('issue', 'unclosed_command_argument', index, len(raw))
            return
        if name in _EXTERNAL:
            yield _Event('issue', 'external_bibliography_deferred', index, end)
        elif name == 'bibitem*':
            yield _Event('issue', 'unsupported_bibitem_form', index, end)
        # Macro definitions/arguments are not executed. Flag a hidden literal
        # bibliography so an empty result is not mistaken for complete coverage.
        elif _BIB_MARKER.search(raw[command.end():end]):
            yield _Event('issue', 'macro_bibliography_deferred', index, end)
        index = end


def _start_region(raw, event):
    opening = _skip_space_comments(raw, event.end)
    closing = _balanced_end(raw, opening) if opening < len(raw) and raw[opening] == '{' else None
    if closing is None:
        warning = Diagnostic('invalid_bibliography_header',
            'Missing/unclosed thebibliography width argument; region is not safe to exclude.',
            event.start, event.end)
        return _Draft(event.start, event.end, False, warnings=[warning])
    return _Draft(event.start, closing, True)


def extract_bibliography(raw: str, *, source: SourceContext | None = None) -> BibliographyResult:
    """Extract literal environments and entries, never edit raw or read files.

    Only exclusion_spans are eligible for removal by a caller. No headings,
    appendices, external .bib files or macros are resolved here.
    """
    if not isinstance(raw, str):
        raise TypeError('raw must be a string')
    if source is not None and not isinstance(source, SourceContext):
        raise TypeError('source must be SourceContext or None')
    regions, entries, unparsed, warnings = [], [], [], []
    seen_keys = set()
    current = None

    def finish(draft, body_end, end, closed):
        region_index = len(regions)
        region_warnings = list(draft.warnings)
        if not closed:
            region_warnings.append(Diagnostic('unclosed_bibliography',
                'Missing bibliography end; do not exclude the tail of the document.', draft.start, end))
        indexes = []
        items = [] if draft.nested else draft.items
        prefix_end = items[0].start if items else body_end
        if draft.body_start < prefix_end:
            unparsed.append(RawFragment('bibliography_preamble' if items else 'bibliography_body',
                raw[draft.body_start:prefix_end], draft.body_start, prefix_end, region_index))
        for index, item in enumerate(items):
            header = item.header
            item_end = items[index + 1].start if index + 1 < len(items) else body_end
            item_warnings = list(header.warnings)
            if header.key is not None:
                if header.key in seen_keys:
                    item_warnings.append(Diagnostic('duplicate_citation_key',
                        'Duplicate key retained as a separate record; deduplication belongs to B3.',
                        item.start, header.content_start))
                seen_keys.add(header.key)
            if not raw[header.content_start:item_end].strip():
                item_warnings.append(Diagnostic('empty_bibitem_content',
                    'Bibitem has no non-whitespace body; metadata was not inferred.',
                    header.content_start, item_end))
            indexes.append(len(entries))
            entries.append(BibItem(header.key, header.key_raw, header.label,
                raw[item.start:item_end], raw[header.content_start:item_end],
                item.start, item_end, header.content_start, region_index, source, tuple(item_warnings)))
            region_warnings.extend(item_warnings)
        if not items and not draft.nested:
            region_warnings.append(Diagnostic('empty_bibliography',
                'No literal top-level bibitem found; raw body retained.', draft.body_start, body_end))
        safe = (closed and draft.header_valid and not draft.ambiguous and not draft.nested
                and all(item.header.valid for item in items))
        regions.append(BibliographyRegion(raw[draft.start:end], draft.start, end,
            draft.body_start, body_end, closed, draft.header_valid, safe,
            tuple(indexes), tuple(region_warnings)))
        warnings.extend(region_warnings)

    for event in _events(raw):
        if event.kind == 'issue':
            warning = Diagnostic(event.name, 'Construct retained; extraction is incomplete for this region.',
                                 event.start, event.end)
            if current is None:
                warnings.append(warning)
            else:
                current.warnings.append(warning)
                current.ambiguous = True
            continue
        if event.kind == 'begin' and event.name == 'thebibliography':
            if current is None:
                current = _start_region(raw, event)
            else:
                current.nested = True
                current.stack.append(event.name)
                current.warnings.append(Diagnostic('nested_bibliography',
                    'Nested bibliographies retained as an unparsed body; no exclusion span returned.',
                    event.start, event.end))
            continue
        if event.kind == 'bibitem':
            if current is None:
                warnings.append(Diagnostic('orphan_bibitem',
                    'Bibitem outside a literal thebibliography was retained, not extracted.', event.start, event.end))
                warnings.extend(event.header.warnings)
            elif len(current.stack) == 1:
                current.items.append(event)
            else:
                current.ambiguous = True
                current.warnings.append(Diagnostic('nested_bibitem_deferred',
                    'Bibitem inside another environment retained without treating it as a top-level entry.',
                    event.start, event.end))
            continue
        if current is None:
            if event.kind == 'end' and event.name == 'thebibliography':
                warnings.append(Diagnostic('orphan_bibliography_end',
                    'Bibliography end without a matching begin retained.', event.start, event.end))
            continue
        if event.kind == 'begin':
            current.stack.append(event.name)
        elif event.kind == 'end':
            if event.name == 'thebibliography' and current.stack.count('thebibliography') == 1:
                if current.stack != ['thebibliography']:
                    current.ambiguous = True
                    current.warnings.append(Diagnostic('mismatched_environment',
                        'Bibliography ended while another environment was still open.', event.start, event.end))
                finish(current, event.start, event.end, True)
                current = None
            elif current.stack and event.name == current.stack[-1]:
                current.stack.pop()
            else:
                current.ambiguous = True
                current.warnings.append(Diagnostic('mismatched_environment',
                    'Mismatched environment inside bibliography retained.', event.start, event.end))
    if current is not None:
        finish(current, len(raw), len(raw), False)
    warnings.sort(key=lambda w: (w.start, w.end, w.code))
    return BibliographyResult(raw, tuple(regions), tuple(entries), tuple(unparsed), tuple(warnings))


@dataclass(frozen=True)
class BibliographyTarget:
    raw: str
    start: int
    end: int
    candidate_paths: tuple[str, ...]
    listed_match: str | None
    status: str


@dataclass(frozen=True)
class BibliographyDeclaration:
    command: str
    raw: str
    start: int
    end: int
    options: tuple[str, ...]
    targets: tuple[BibliographyTarget, ...]
    source_spans: tuple[MappedSourceSpan, ...]


@dataclass(frozen=True)
class BibliographyDependencies:
    raw: str
    declarations: tuple[BibliographyDeclaration, ...]
    warnings: tuple[Diagnostic, ...]


def _relative(path):
    if not isinstance(path, str) or not path or path.startswith('/') or re.match(r'^[A-Za-z]:', path) or '\\' in path:
        return None
    parts = []
    for part in PurePosixPath(path).parts:
        if part == '..':
            if not parts:
                return None
            parts.pop()
        elif part != '.':
            parts.append(part)
    return '/'.join(parts)


def inspect_bibliography_dependencies(raw, *, listed_bib_files=(), root_file=None, source_map=None):
    """Find literal bibliography/addbibresource/printbibliography declarations.

    Paths supplied by A1 must be source-root relative. No files are opened,
    matched resources are not read, and an unmatched target is not a crawl error.
    """
    if not isinstance(raw, str):
        raise TypeError('raw must be str')
    if source_map is not None and (not isinstance(source_map, RawSourceMap) or source_map.raw != raw):
        raise ValueError('source_map must match exact bibliography input')
    listed = set()
    for path in listed_bib_files:
        normalized = _relative(path)
        if normalized is None:
            raise ValueError('listed_bib_files must contain source-root-relative POSIX paths')
        listed.add(normalized)
    if root_file is not None and _relative(root_file) is None:
        raise ValueError('root_file must be source-root relative')
    declarations, warnings = [], []

    def warn(code, message, start, end):
        warnings.append(Diagnostic(code, message, start, end))

    for event in _events(raw):
        if event.kind == 'issue' and event.name == 'conditional_content_deferred' and re.search(
                r'\\(?:bibliography|addbibresource|printbibliography)\b', raw[event.start:event.end]):
            warn('bibliography_dependency_conditional',
                 'Conditional bibliography requires explicit resource selection.', event.start, event.end)
            continue
        if event.kind != 'issue' or event.name not in {'external_bibliography_deferred', 'unclosed_command_argument'}:
            continue
        command = _COMMAND.match(raw, event.start)
        if command is None or command[1] not in {'bibliography', 'addbibresource', 'printbibliography'}:
            continue
        name = command[1]
        pos = _skip_space_comments(raw, command.end())
        options = []
        while pos < len(raw) and raw[pos] == '[':
            end = _balanced_end(raw, pos)
            if end is None:
                warn('bibliography_dependency_malformed', 'Unclosed declaration option.', pos, len(raw))
                break
            options.append(raw[pos:end])
            pos = _skip_space_comments(raw, end)
        else:
            targets = []
            if name == 'printbibliography':
                end = pos if options else command.end()
                warn('bibliography_resources_require_context',
                     'Print boundary does not identify resources; inspect A1 preamble declarations too.', event.start, end)
            else:
                end = _balanced_end(raw, pos) if pos < len(raw) and raw[pos] == '{' else None
                if end is None:
                    warn('bibliography_dependency_malformed', 'Expected a literal braced resource list.', event.start, max(command.end(), pos))
                    continue
                content = raw[pos + 1:end - 1]
                pieces = content.split(',') if name == 'bibliography' else [content]
                offset = pos + 1
                for text in pieces:
                    left = len(text) - len(text.lstrip())
                    right = max(left, len(text.rstrip()))
                    start, stop = offset + left, offset + right
                    target = raw[start:stop]
                    offset += len(text) + 1
                    paths, match = (), None
                    if not target or re.search(r'[\\%{}\s\x00-\x1f]', target):
                        status = 'dynamic_or_unsupported_target'
                    elif target.startswith('/') or re.match(r'^[A-Za-z]:|^[A-Za-z]+://', target):
                        status = 'unsafe_or_external_target'
                    else:
                        filename = target if PurePosixPath(target).suffix else target + '.bib'
                        bases = {''}  # Advisory direct source-root-relative interpretation.
                        if root_file:
                            bases.add(str(PurePosixPath(root_file).parent))
                        if source_map is not None:
                            origins = source_map.resolve(event.start, end)
                            origin_paths = {span.path for span in origins}
                            if len(origin_paths) == 1:
                                bases.add(str(PurePosixPath(next(iter(origin_paths))).parent))
                        possible = {_relative(str(PurePosixPath(base) / filename)) for base in bases}
                        possible.discard(None)
                        paths = tuple(sorted(possible & listed))
                        if len(paths) == 1:
                            status, match = 'one_listed_candidate', paths[0]
                        elif len(paths) > 1:
                            status = 'ambiguous_listed_candidates'
                        elif not possible:
                            status = 'unsafe_or_external_target'
                        else:
                            status = 'not_in_listed_bib_files'
                    targets.append(BibliographyTarget(target, start, stop, paths, match, status))
                    if status != 'one_listed_candidate':
                        warn('bibliography_dependency_' + status,
                             'Resource retained; no unique listed-file candidate selected.', start, stop)
            declarations.append(BibliographyDeclaration(name, raw[event.start:end], event.start, end,
                tuple(options), tuple(targets), source_map.resolve(event.start, end) if source_map else ()))
    return BibliographyDependencies(raw, tuple(declarations), tuple(warnings))
