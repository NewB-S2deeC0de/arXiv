"""Read the compilation path of one LaTeX source version without editing it."""

from dataclasses import dataclass, field
from pathlib import Path
import re


@dataclass(frozen=True)
class SourceSpan:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class GatheredSegment:
    text: str
    origin: SourceSpan
    kind: str = 'source_text'


@dataclass(frozen=True)
class GatherDiagnostic:
    code: str
    message: str
    origin: SourceSpan | None
    severity: str = 'warning'


@dataclass
class GatheredVersion:
    arxiv_id: str
    version: int
    status: str
    root_file: str | None
    segments: list[GatheredSegment] = field(default_factory=list)
    used_tex_files: list[str] = field(default_factory=list)
    bib_files: list[str] = field(default_factory=list)
    diagnostics: list[GatherDiagnostic] = field(default_factory=list)


_VERBATIM = frozenset({'verbatim', 'verbatim*', 'lstlisting', 'minted', 'comment'})
_MACRO_DEFINITIONS = frozenset({'newcommand', 'renewcommand', 'providecommand', 'DeclareRobustCommand'})
_COMMAND_NAME = re.compile(r'[A-Za-z@]+')
_LITERAL_INCLUDEONLY = re.compile(r'[A-Za-z0-9_./-]*(?:\s*,\s*[A-Za-z0-9_./-]+)*')


class _ResourceLimit(Exception):
    pass


def _braced_end(text, start):
    if start >= len(text) or text[start] != '{': return None
    depth = 1
    i = start + 1
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text) and text[i + 1] in '{}':
            i += 2
            continue
        if text[i] == '{': depth += 1
        elif text[i] == '}':
            depth -= 1
            if not depth: return i + 1
        i += 1
    return None


def _commands(text):
    """Yield structural commands outside comments and literal-text regions."""
    i = 0
    while i < len(text):
        if text[i] == '%':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text[i] != '\\':
            i += 1
            continue
        start = i
        i += 1
        if i == len(text):
            break
        match = _COMMAND_NAME.match(text, i)
        if match:
            name = match.group()
            i += len(name)
        else:
            name = text[i]
            i += 1
        if name == 'verb':
            if i < len(text) and text[i] == '*':
                i += 1
            if i < len(text):
                closing = text.find(text[i], i + 1)
                i = len(text) if closing < 0 else closing + 1
            continue
        if name in _MACRO_DEFINITIONS:
            if i < len(text) and text[i] == '*': i += 1
            while i < len(text) and text[i].isspace(): i += 1
            macro_end = _braced_end(text, i)
            if macro_end is None and i < len(text) and text[i] == '\\':
                i += 1
                control = _COMMAND_NAME.match(text, i)
                macro_end = control.end() if control else min(i + 1, len(text))
            if macro_end is None:
                yield 'macro_definition', start, i, None
                continue
            i = macro_end
            for _ in range(2):
                while i < len(text) and text[i].isspace(): i += 1
                if i < len(text) and text[i] == '[':
                    end = text.find(']', i + 1)
                    i = len(text) if end < 0 else end + 1
            while i < len(text) and text[i].isspace(): i += 1
            body_end = _braced_end(text, i)
            if body_end is not None: i = body_end
            if '\\input' in text[start:i] or '\\include' in text[start:i]:
                yield 'macro_definition', start, i, None
            continue
        if name in ('iftrue', 'iffalse', 'else', 'fi') or name.startswith('if'):
            yield name if name in ('iftrue', 'iffalse', 'else', 'fi') else 'unknown_if', start, i, None
            continue
        if name not in ('documentclass', 'input', 'include', 'includeonly', 'begin', 'end'):
            arg_at = i
            while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
            if arg_at < len(text) and text[arg_at] == '*': arg_at += 1
            while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
            if arg_at < len(text) and text[arg_at] == '[':
                bracket_end = text.find(']', arg_at + 1)
                if bracket_end >= 0: arg_at = bracket_end + 1
            while arg_at < len(text) and text[arg_at].isspace(): arg_at += 1
            opaque_end = _braced_end(text, arg_at)
            if opaque_end is not None:
                if '\\input' in text[arg_at:opaque_end] or '\\include' in text[arg_at:opaque_end]:
                    yield 'opaque_argument', start, opaque_end, None
                i = opaque_end
            continue
        arg_start = i
        while arg_start < len(text) and text[arg_start].isspace():
            arg_start += 1
        if name == 'documentclass' and arg_start < len(text) and text[arg_start] == '[':
            option_end = text.find(']', arg_start + 1)
            if option_end >= 0:
                arg_start = option_end + 1
                while arg_start < len(text) and text[arg_start].isspace():
                    arg_start += 1
        if arg_start >= len(text) or text[arg_start] != '{':
            yield name, start, i, None
            continue
        depth = 1
        end = arg_start + 1
        while end < len(text) and depth:
            if text[end] == '{': depth += 1
            elif text[end] == '}': depth -= 1
            end += 1
        if depth:
            yield name, start, i, None
            continue
        argument = text[arg_start + 1:end - 1]
        i = end
        if name == 'begin' and argument in _VERBATIM:
            close = f'\\end{{{argument}}}'
            closing = text.find(close, i)
            i = len(text) if closing < 0 else closing + len(close)
            continue
        yield name, start, end, argument


def _conditional_events(text):
    """Attach branch visibility to commands; unknown conditions stay unverified."""
    stack = []
    active = True
    for name, start, end, argument in _commands(text):
        before = active
        if name in ('iftrue', 'iffalse', 'unknown_if'):
            stack.append((active, name, False))
            active = active and (name == 'iftrue')
        elif name == 'else':
            if stack:
                parent, kind, seen_else = stack[-1]
                stack[-1] = (parent, kind, True)
                active = parent and kind == 'iffalse' and not seen_else
                if seen_else: name = 'unmatched_else'
            else:
                name = 'unmatched_else'
        elif name == 'fi':
            if stack: active = stack.pop()[0]
            else: name = 'unmatched_fi'
        yield name, start, end, argument, before, active
    if stack:
        yield 'unclosed_if', len(text), len(text), None, active, active


def gather_version(arxiv_id: str, version: int, source_dir: Path, *,
                   max_file_bytes: int = 32 * 1024 * 1024,
                   max_output_bytes: int = 64 * 1024 * 1024) -> GatheredVersion:
    """Gather root and literal include targets, preserving file-relative spans."""
    source_dir = Path(source_dir).resolve()
    files = sorted(p for p in source_dir.rglob('*.tex')
                   if p.is_file() and p.resolve().is_relative_to(source_dir))
    bib_files = sorted(p.relative_to(source_dir).as_posix() for p in source_dir.rglob('*.bib')
                       if p.is_file() and p.resolve().is_relative_to(source_dir))
    result = GatheredVersion(arxiv_id, version, 'unparseable', None, bib_files=bib_files)
    contents = {}

    def read(path):
        if path not in contents:
            if path.stat().st_size > max_file_bytes:
                raise _ResourceLimit(f'Input file exceeds {max_file_bytes} bytes')
            contents[path] = path.read_text(encoding='utf8')
        return contents[path]

    roots = []
    unreadable = []
    oversized = []
    for path in files:
        try:
            if any(name == 'documentclass' and arg is not None and before
                   for name, _, _, arg, before, _ in _conditional_events(read(path))):
                roots.append(path)
        except UnicodeError:
            unreadable.append(path.relative_to(source_dir).as_posix())
        except _ResourceLimit:
            oversized.append(path.relative_to(source_dir).as_posix())
    if len(roots) != 1:
        for path in unreadable:
            result.diagnostics.append(GatherDiagnostic(
                'decode_error', f'Cannot decode {path}', SourceSpan(path, 0, 0), 'error'))
        for path in oversized:
            result.diagnostics.append(GatherDiagnostic(
                'resource_limit', f'Cannot scan oversized {path}', SourceSpan(path, 0, 0), 'error'))
        code = 'no_root' if not roots else 'ambiguous_root'
        candidates = [p.relative_to(source_dir).as_posix() for p in roots]
        result.diagnostics.append(GatherDiagnostic(code, f'Root candidates: {candidates}', None, 'error'))
        return result
    root = roots[0]
    result.root_file = root.relative_to(source_dir).as_posix()
    result.status = 'ready'
    output_bytes = 0
    root_commands = list(_conditional_events(read(root)))
    markers = [(name, start) for name, start, _, arg, before, _ in root_commands
               if before and ((name == 'begin' and arg == 'document') or
                              (name == 'end' and arg == 'document'))]
    begins = [pos for name, pos in markers if name == 'begin']
    ends = [pos for name, pos in markers if name == 'end']
    classes = [start for name, start, _, arg, before, _ in root_commands
               if name == 'documentclass' and arg is not None and before]
    if (len(classes) != 1 or len(begins) != 1 or len(ends) != 1 or
            not classes[0] < begins[0] < ends[0]):
        result.status = 'partial'
        result.diagnostics.append(GatherDiagnostic(
            'document_structure', 'Missing, duplicate, or out-of-order document markers', None))
    includeonly = None
    for name, start, end, arg, before, _ in root_commands:
        if name != 'includeonly' or not before: continue
        if arg is None or not _LITERAL_INCLUDEONLY.fullmatch(arg) or '\\' in arg:
            includeonly = False
            result.status = 'partial'
            result.diagnostics.append(GatherDiagnostic(
                'dynamic_includeonly', 'Cannot resolve includeonly list',
                SourceSpan(result.root_file, start, end)))
        elif includeonly is not False:
            includeonly = {item.strip() for item in arg.split(',') if item.strip()}

    def add_segment(path, text, start, end):
        nonlocal output_bytes
        if start < end:
            segment = text[start:end]
            size = len(segment.encode('utf8'))
            if output_bytes + size > max_output_bytes:
                raise _ResourceLimit(f'Expanded output exceeds {max_output_bytes} bytes')
            output_bytes += size
            origin = SourceSpan(path.relative_to(source_dir).as_posix(), start, end)
            result.segments.append(GatheredSegment(segment, origin))

    def visit(path, stack):
        relative = path.relative_to(source_dir).as_posix()
        if relative not in result.used_tex_files: result.used_tex_files.append(relative)
        text = read(path)
        cursor = 0
        for name, start, end, argument, before, after in _conditional_events(text):
            if name in ('iftrue', 'iffalse', 'unknown_if', 'else', 'fi',
                        'unmatched_else', 'unmatched_fi'):
                if before: add_segment(path, text, cursor, start)
                cursor = end
                if name in ('unknown_if', 'unmatched_else', 'unmatched_fi'):
                    result.status = 'partial'
                    result.diagnostics.append(GatherDiagnostic(
                        'unsupported_conditional', 'Conditional cannot be evaluated',
                        SourceSpan(relative, start, end)))
                continue
            if name == 'unclosed_if':
                result.status = 'partial'
                result.diagnostics.append(GatherDiagnostic(
                    'unsupported_conditional', 'Conditional has no matching fi',
                    SourceSpan(relative, start, end)))
                continue
            if not before: continue
            if name in ('macro_definition', 'opaque_argument'):
                result.status = 'partial'
                result.diagnostics.append(GatherDiagnostic(
                    'dynamic_macro' if name == 'macro_definition' else 'opaque_argument',
                    'Structural command in unevaluated argument or macro',
                    SourceSpan(relative, start, end)))
                continue
            if name not in ('input', 'include'): continue
            add_segment(path, text, cursor, start)
            cursor = end
            origin = SourceSpan(relative, start, end)
            if name == 'include' and (includeonly is False or
                                      (includeonly is not None and argument not in includeonly)):
                if includeonly is False: add_segment(path, text, start, end)
                continue
            if not argument or '\\' in argument:
                code = 'dynamic_include'
                target = None
            else:
                requested = Path(argument)
                absolute = requested.is_absolute()
                raw_target = path.parent / requested
                options = [raw_target]
                if not requested.suffix: options.append(raw_target.with_suffix('.tex'))
                target = next((candidate.resolve() for candidate in options
                               if candidate.is_file() and candidate.resolve().is_relative_to(source_dir)),
                              options[-1].resolve())
                if absolute or not target.is_relative_to(source_dir): code = 'unsafe_include'
                elif target in stack: code = 'include_cycle'
                elif len(stack) >= 64: code = 'depth_limit'
                elif not target.is_file(): code = 'missing_include'
                else:
                    try:
                        read(target)
                        code = None
                    except UnicodeError:
                        code = 'decode_error'
                    except _ResourceLimit:
                        code = 'resource_limit'
            if code:
                result.status = 'partial'
                result.diagnostics.append(GatherDiagnostic(code, f'Cannot expand {argument!r}', origin))
                add_segment(path, text, start, end)
            else:
                visit(target, (*stack, target))
        add_segment(path, text, cursor, len(text))

    try:
        visit(root, (root,))
    except _ResourceLimit as exc:
        result.status = 'partial'
        result.diagnostics.append(GatherDiagnostic('resource_limit', str(exc), None))
    return result
