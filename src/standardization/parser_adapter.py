"""B-side adapters for the provisional A2 contract dated 2026-11-04.

Parser classes are consumed by attributes, without a dependency on A's package.
Container spans stay opaque; optional explicit A1 maps enable exact resolution.
"""
from dataclasses import dataclass, replace, is_dataclass
from copy import deepcopy

from .bibliography import BibliographyDependencies, extract_bibliography, inspect_bibliography_dependencies
from .models import BibliographyResult, Diagnostic, SourceContext
from .normalize import normalize_block_math, normalize_figure, normalize_inline_math, normalize_text
from .source_mapping import RawSourceMap, source_map_from_gathered, source_map_from_spans


@dataclass(frozen=True)
class BoundDiagnostic:
    code: str
    message: str
    start: int
    end: int
    offset_basis: str
    source_spans: tuple
    severity: str = 'warning'
    source_mapping: str = 'container_only'


@dataclass(frozen=True)
class ParserNormalizationResult:
    occurrence_id: int
    kind: str
    raw: str
    content: str
    source_spans: tuple
    upstream_warnings: tuple
    diagnostics: tuple[BoundDiagnostic, ...]
    applied: bool


@dataclass(frozen=True)
class ParserBibliographyResult:
    raw: str
    source_spans: tuple
    bibliography: BibliographyResult
    diagnostics: tuple[BoundDiagnostic, ...]
    source_map: RawSourceMap | None = None
    dependencies: BibliographyDependencies | None = None
    block_kind: str = 'unresolved'


@dataclass(frozen=True)
class GatheredBibliographyResult:
    arxiv_id: str
    version: int
    status: str
    root_file: str | None
    used_tex_files: tuple
    bib_files: tuple
    upstream_diagnostics: tuple
    result: ParserBibliographyResult | None


@dataclass(frozen=True)
class NormalizedA3Hierarchy:
    hierarchy: object
    nodes: tuple[ParserNormalizationResult, ...]
    diagnostics: tuple[BoundDiagnostic, ...]
    source_mapping_failures: int


@dataclass(frozen=True)
class VersionBibliographyInspection:
    arxiv_id: str
    version: int
    status: str
    blocks: tuple[ParserBibliographyResult, ...]
    gathered_dependencies: BibliographyDependencies
    gather_diagnostics: tuple
    parse_diagnostics: tuple
    source_mapping_failures: int


def _raw_and_spans(value):
    try:
        raw, spans = value.raw, value.source_spans
    except AttributeError as exc:
        raise TypeError('Expected an object with raw and source_spans attributes') from exc
    if not isinstance(raw, str):
        raise TypeError('raw must be a string')
    if not isinstance(spans, (list, tuple)):
        raise TypeError('source_spans must be an ordered list/tuple; span schema is opaque')
    return raw, tuple(spans)


def _bind(warnings, spans, basis, source_map=None):
    bound = []
    for warning in warnings:
        mapped = source_map.resolve(warning.start, warning.end) if source_map is not None else spans
        mapping_kind = ('exact_segments' if mapped else 'unmapped_empty') if source_map is not None else 'container_only'
        bound.append(BoundDiagnostic(warning.code, warning.message, warning.start, warning.end,
                     basis, mapped, source_mapping=mapping_kind))
    return tuple(bound)


def _check_source_map(raw, source_map):
    if source_map is not None:
        if not isinstance(source_map, RawSourceMap):
            raise TypeError('source_map must be RawSourceMap or None')
        if source_map.raw != raw:
            raise ValueError('Source map raw differs from input; regenerate mapping after upstream edits')


def _replace_fields(value, **fields):
    """Replace dataclass fields (including frozen C nodes) on an owned copy."""
    if is_dataclass(value):
        return replace(value, **fields)
    for name, item in fields.items():
        setattr(value, name, item)
    return value


def normalize_parser_leaf(node, *, after_a3: bool, mode: str = 'preserve', source_map=None):
    """Return content/diagnostics beside a node; never edit node/tree/provenance.

    Caller explicitly declares post-A3 input and selects a normalization mode.
    A3 dispatch is provided by normalize_a3_hierarchy. Structural
    and nonleaf nodes are always preserved. No traversal or sentence splitting.
    """
    if type(after_a3) is not bool:
        raise TypeError('after_a3 must be a bool')
    modes = {'prose': normalize_text, 'inline_math': normalize_inline_math,
             'block_math': normalize_block_math, 'figure': normalize_figure}
    if mode not in {*modes, 'preserve'}:
        raise ValueError('mode must be prose, inline_math, block_math, figure or preserve')
    raw, spans = _raw_and_spans(node)
    _check_source_map(raw, source_map)
    try:
        occurrence_id, kind, children, upstream = node.occurrence_id, node.kind, node.children, node.warnings
    except AttributeError as exc:
        raise TypeError('Expected ParseNode occurrence_id/kind/children/warnings attributes') from exc
    if type(occurrence_id) is not int or not isinstance(kind, str):
        raise TypeError('occurrence_id must be int and kind must be str')
    if not isinstance(children, (list, tuple)) or not isinstance(upstream, (list, tuple)):
        raise TypeError('children/warnings must be lists or tuples')
    content, warnings, applied = raw, (), False
    if not after_a3:
        warnings = (Diagnostic('normalization_requires_a3',
            'A2 paragraph raw may still contain figures/math; normalize only after A3.', 0, len(raw)),)
    elif children or kind in {'document', 'heading', 'list', 'item'}:
        warnings = (Diagnostic('structural_node_preserved',
            'Structural/nonleaf node retained; caller must pass the appropriate A3 leaf.', 0, len(raw)),)
    elif mode != 'preserve' and kind in {'figure', 'table', 'verbatim', 'code'} and not (kind == 'figure' and mode == 'figure'):
        warnings = (Diagnostic('parser_region_policy_deferred',
            'Figure/table/code policy is not enabled by this contract; raw retained.', 0, len(raw)),)
    elif mode != 'preserve':
        result = modes[mode](raw)
        content, warnings, applied = result.content, result.warnings, True
    return ParserNormalizationResult(occurrence_id, kind, raw, content, spans,
                                     tuple(upstream), _bind(warnings, spans, 'node_raw', source_map), applied)


def normalize_a3_hierarchy(hierarchy, *, source_texts=None):
    """Fill leaf content on a deep copy of A3, preserving all upstream metadata.

    Structural nodes/paragraph parents retain existing content. An unsplit A3
    paragraph or failed source map is copied as raw content with diagnostics.
    A's status is never upgraded; B diagnostics remain in this wrapper, not
    inserted into A's warnings:list[str] or ParseDiagnostic classes.
    """
    if hierarchy.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown HierarchyResult.status')
    if hierarchy.status == 'unparseable' and hierarchy.root is not None:
        raise ValueError('Unparseable A3 input must not contain a usable root')
    result = deepcopy(hierarchy)
    nodes, diagnostics = [], []
    mapping_failures = 0
    if result.root is None:
        if hierarchy.status != 'unparseable':
            raise ValueError('Ready/partial A3 input must contain a document root')
        return NormalizedA3Hierarchy(result, (), (), 0)
    if hierarchy.status != 'unparseable' and result.root.kind != 'document':
        raise ValueError('Expected version-local document root')
    seen = set()

    def visit(node):
        nonlocal mapping_failures
        if node.occurrence_id in seen:
            raise ValueError('Duplicate occurrence_id in A3 version')
        seen.add(node.occurrence_id)
        if not hasattr(node, 'content'):
            raise TypeError('A3 node must expose content separately from raw')
        if node.children:
            children = [visit(child) for child in node.children]
            return _replace_fields(node, children=tuple(children) if isinstance(node.children, tuple) else children)
        if node.kind in {'document', 'heading', 'list', 'item'}:
            return node
        raw, spans = _raw_and_spans(node)
        source_map = None
        issue = None
        if source_texts is not None:
            try:
                source_map = source_map_from_spans(raw, spans, source_texts)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                mapping_failures += 1
                issue = Diagnostic('normalization_source_mapping_unavailable',
                                   str(exc), 0, len(raw))
        modes = {'sentence': 'prose', 'block_formula': 'block_math', 'figure': 'figure'}
        mode = modes.get(node.kind, 'preserve') if issue is None else 'preserve'
        normalized = normalize_parser_leaf(node, after_a3=True, mode=mode, source_map=source_map)
        extra = ()
        if issue is not None:
            extra = _bind((issue,), spans, 'node_raw')
        elif node.kind not in modes:
            code = 'a3_paragraph_unresolved' if node.kind == 'paragraph' else 'a3_leaf_policy_deferred'
            extra = _bind((Diagnostic(code, 'Unresolved/unsupported A3 leaf retained as raw content.',
                                      0, len(raw)),), spans, 'node_raw', source_map)
        if extra:
            normalized = replace(normalized, diagnostics=normalized.diagnostics + extra)
        nodes.append(normalized)
        diagnostics.extend(normalized.diagnostics)
        return _replace_fields(node, content=normalized.content)

    result = _replace_fields(result, root=visit(result.root))
    return NormalizedA3Hierarchy(result, tuple(nodes), tuple(diagnostics), mapping_failures)


def extract_parser_bibliography(block, *, arxiv_id: str, version: int, source_map=None,
                                listed_bib_files=(), root_file=None):
    """Consume A2's complete BibliographyBlock without modifying the body tree.

    The block can include a References heading moved by A2. Returned entry
    offsets refer to block.raw. Ordered source_spans are retained beside it.
    No file path is guessed from an opaque span or assigned to a merged entry.
    """
    raw, spans = _raw_and_spans(block)
    _check_source_map(raw, source_map)
    if not isinstance(arxiv_id, str) or not arxiv_id:
        raise ValueError('arxiv_id must be a nonempty string supplied by the caller')
    source = SourceContext(arxiv_id=arxiv_id, version=version)
    if version is None:
        raise ValueError('version must be a positive integer supplied by the caller')
    result = extract_bibliography(raw, source=source)
    dependencies = inspect_bibliography_dependencies(raw, source_map=source_map,
                                listed_bib_files=listed_bib_files, root_file=root_file)
    block_kind = ('mixed' if dependencies.declarations else 'bibitems') if result.regions else (
                 'external' if dependencies.declarations else 'unresolved')
    return ParserBibliographyResult(raw, spans, result,
        _bind(result.warnings + dependencies.warnings, spans, 'bibliography_block_raw', source_map),
        source_map, dependencies, block_kind)


def extract_gathered_bibliography(gathered):
    """Offline A1->B2 bridge for verification, not replacement for A2/C3 pipeline.

    Keep A1 status/diagnostics and listed bib paths; never assert .bib was used.
    Unparseable input returns no B2 result rather than a false empty success.
    """
    if gathered.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown GatheredVersion.status')
    result = None
    if gathered.status != 'unparseable':
        source_map = source_map_from_gathered(gathered)
        # Minimal block-shaped object without importing or changing A's classes.
        @dataclass(frozen=True)
        class _GatheredBlock:
            raw: str
            source_spans: tuple
        block = _GatheredBlock(source_map.raw, source_map.resolve(0, len(source_map.raw)))
        result = extract_parser_bibliography(block, arxiv_id=gathered.arxiv_id,
            version=gathered.version, source_map=source_map,
            listed_bib_files=gathered.bib_files, root_file=gathered.root_file)
    return GatheredBibliographyResult(gathered.arxiv_id, gathered.version, gathered.status,
        gathered.root_file, tuple(gathered.used_tex_files), tuple(gathered.bib_files),
        tuple(gathered.diagnostics), result)


def inspect_version_bibliography(hierarchy, gathered, *, source_texts=None):
    """Inspect A3 blocks plus A1 preamble dependencies, without reading .bib.

    Retain both stages' diagnostics and exact version context. This is a
    B-side result, not final C3 tables or a resource-selection policy.
    """
    if (hierarchy.arxiv_id, hierarchy.version) != (gathered.arxiv_id, gathered.version):
        raise ValueError('A1/A3 publication and version must agree')
    if hierarchy.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown HierarchyResult.status')
    if gathered.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown GatheredVersion.status')
    if gathered.status == 'unparseable' and hierarchy.status != 'unparseable':
        raise ValueError('Cannot recover unparseable A1 status without a new gather result')
    if gathered.status == 'partial' and hierarchy.status == 'ready':
        raise ValueError('A3 must not upgrade partial A1 coverage to ready')
    mapping = source_map_from_gathered(gathered)
    dependencies = inspect_bibliography_dependencies(mapping.raw, source_map=mapping,
        listed_bib_files=gathered.bib_files, root_file=gathered.root_file)
    blocks, failures = [], 0
    for block in hierarchy.bibliography:
        block_map, issue = None, None
        if source_texts is not None:
            try:
                block_map = source_map_from_spans(block.raw, block.source_spans, source_texts)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                failures += 1
                issue = Diagnostic('bibliography_source_mapping_unavailable', str(exc), 0, len(block.raw))
        result = extract_parser_bibliography(block, arxiv_id=hierarchy.arxiv_id,
            version=hierarchy.version, source_map=block_map,
            listed_bib_files=gathered.bib_files, root_file=gathered.root_file)
        if issue is not None:
            result = replace(result, diagnostics=result.diagnostics +
                             _bind((issue,), tuple(block.source_spans), 'bibliography_block_raw'))
        blocks.append(result)
    return VersionBibliographyInspection(hierarchy.arxiv_id, hierarchy.version, hierarchy.status,
        tuple(blocks), dependencies, tuple(gathered.diagnostics), tuple(hierarchy.diagnostics), failures)
