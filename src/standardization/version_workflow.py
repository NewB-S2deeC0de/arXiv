"""B-side version orchestration; no resource or C publication policy decisions."""
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .parser_adapter import (inspect_version_bibliography, _replace_fields)
from .bibitem_metadata import extract_bibitem_metadata
from .c_bibentry_adapter import prepare_c_bibentry
from .reference_candidates import make_reference_occurrence
from .citations import remap_citations
from .bibtex_reader import BibtexReadResult, read_bibtex
from .c_bibentry_adapter import prepare_bibtex_entry
from .models import SourceContext
from .models import Diagnostic
from .normalize import normalize_text
from .source_mapping import MappedSourceSpan, RawSourceMap, SourceMapPiece


@dataclass(frozen=True)
class BibResourceRead:
    path: str
    parsed: BibtexReadResult | None
    diagnostic: Diagnostic | None = None


@dataclass(frozen=True)
class PreparedVersionBibliography:
    inspection: object
    occurrences: tuple
    metadata: tuple
    resources: tuple[BibResourceRead, ...] = ()
    selection_diagnostics: tuple[Diagnostic, ...] = ()


def prepare_version_bibliography(hierarchy, gathered, *, source_texts=None,
                                 source_dir=None, selected_bib_files=()):
    """Stage A3 bibitems and explicitly selected A1-listed BibTeX resources.

    Accept A1 GatheredVersion or C GatheredSource carrying its raw A1 result.
    Pass None to select uniquely matched literal declarations, or provide a list.
    Both source types retain separate occurrences and provenance for B3.
    """
    if not hasattr(gathered, 'segments'):
        gathered = getattr(gathered, 'raw', None)
        if gathered is None or not hasattr(gathered, 'segments'):
            raise ValueError('Bibliography extraction requires the raw A1 GatheredVersion')
    inspection = inspect_version_bibliography(hierarchy, gathered, source_texts=source_texts)
    selection_diagnostics = []
    if selected_bib_files is None:
        selected_bib_files = []
        dependencies = inspection.gathered_dependencies
        selection_diagnostics.extend(w for w in dependencies.warnings
                                     if w.code != 'bibliography_resources_require_context')
        if inspection.status != 'unparseable':
            for declaration in dependencies.declarations:
                if declaration.options:
                    selection_diagnostics.append(Diagnostic('bib_resource_options_require_selection',
                        'Resource or print options require explicit resource selection.',
                        declaration.start, declaration.end))
                else:
                    selected_bib_files.extend(target.listed_match for target in declaration.targets
                                              if target.listed_match is not None)
            if selection_diagnostics:
                selected_bib_files = []
            elif any(d.command == 'printbibliography' for d in dependencies.declarations) and not selected_bib_files:
                selection_diagnostics.extend(dependencies.warnings)
    if isinstance(selected_bib_files, (str, bytes)):
        raise TypeError('selected_bib_files must be an iterable of relative paths')
    selected = tuple(selected_bib_files)
    if any(not isinstance(name, str) for name in selected):
        raise TypeError('Selected BibTeX paths must be strings')
    selected = tuple(dict.fromkeys(selected))
    if selected and (source_dir is None or inspection.status == 'unparseable'):
        raise ValueError('Selected BibTeX resources require a source directory and usable hierarchy')
    occurrences, metadata_results = [], []
    for block_index, block in enumerate(inspection.blocks):
        for entry_index, entry in enumerate(block.bibliography.entries):
            metadata = extract_bibitem_metadata(entry)
            prepared = prepare_c_bibentry(entry, metadata=dict(metadata.metadata))
            occurrences.append(make_reference_occurrence(prepared,
                occurrence_id=f'block:{block_index}:entry:{entry_index}',
                metadata_result=metadata, source_map=block.source_map))
            metadata_results.append(metadata)
    resources = []
    if selected:
        root = Path(source_dir).resolve()
        inventory = set(gathered.bib_files)
        for name in selected:
            path = PurePosixPath(name)
            if (name not in inventory or path.is_absolute() or '..' in path.parts
                    or '\\' in name or ':' in name or path.as_posix() != name
                    or path.suffix.lower() != '.bib'):
                resources.append(BibResourceRead(name, None, Diagnostic(
                    'bib_resource_not_allowed', 'Expected an A1-listed relative .bib path.', 0, 0)))
                continue
            try:
                target = (root / name).resolve()
                if not target.is_relative_to(root):
                    resources.append(BibResourceRead(name, None, Diagnostic(
                        'bib_resource_outside_source', 'Selected resource resolves outside source directory.', 0, 0)))
                    continue
                raw = target.read_text(encoding='utf-8')
            except (OSError, UnicodeError, ValueError) as exc:
                resources.append(BibResourceRead(name, None, Diagnostic(
                    'bib_resource_read_failed', str(exc), 0, 0)))
                continue
            parsed = read_bibtex(raw, source=SourceContext(inspection.arxiv_id, inspection.version, name))
            resources.append(BibResourceRead(name, parsed))
            pieces = (SourceMapPiece(0, len(raw), MappedSourceSpan(name, 0, len(raw))),) if raw else ()
            source_map = RawSourceMap(raw, pieces)
            for index, entry in enumerate(parsed.entries):
                occurrences.append(make_reference_occurrence(prepare_bibtex_entry(entry),
                    occurrence_id=f'bib:{name}:{index}', source_map=source_map))
    return PreparedVersionBibliography(inspection, tuple(occurrences), tuple(metadata_results),
                                       tuple(resources), tuple(selection_diagnostics))


def default_structural_content(node):
    """Use heading titles and let container children carry body content."""
    if node.kind == 'heading':
        return normalize_text(node.attrs.get('title', node.raw)).content
    return '' if node.children or node.kind in {'document', 'list'} else node.raw


def to_c_tree(hierarchy, *, node_factory, structural_content=default_structural_content):
    """Project a normalized rich tree with an explicit structural-content policy.

    Caller keeps rich hierarchy/diagnostics alongside this lossy C2 tree. Never
    fall back to raw for an unnormalized leaf. Does not export partial by itself.
    structural_content(node) handles containers/heading with content=None.
    """
    if not callable(node_factory) or not callable(structural_content):
        raise TypeError('Explicit node factory and structural-content policy required')
    if hierarchy.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown hierarchy status')
    if hierarchy.root is None:
        if hierarchy.status != 'unparseable':
            raise ValueError('Ready/partial hierarchy requires a root')
        return None
    if hierarchy.status == 'unparseable':
        raise ValueError('Unparseable hierarchy must not expose a usable root')
    seen = set()

    def convert(node):
        if node.occurrence_id in seen:
            raise ValueError('Duplicate version-local occurrence id')
        seen.add(node.occurrence_id)
        content = node.content
        structural = bool(node.children) or node.kind in {'document', 'heading', 'list', 'item'}
        if content is None:
            if not structural:
                raise ValueError('Leaf content is not finalized; call B1 first')
            content = structural_content(deepcopy(node))
        if not isinstance(content, str):
            raise TypeError('C tree content must be str')
        return node_factory(kind=node.kind, content=content,
                            children=[convert(child) for child in node.children])

    return convert(hierarchy.root)


@dataclass(frozen=True)
class NodeCitationRemap:
    occurrence_id: int
    kind: str
    result: object
    offset_basis: str = 'node_content_before_remap'


@dataclass(frozen=True)
class RemappedHierarchy:
    hierarchy: object
    nodes: tuple[NodeCitationRemap, ...]


def remap_a3_hierarchy(hierarchy, mapping):
    """Simultaneous version-scoped remap on normalized leaf content, raw intact.

    Edit offsets refer to pre-remap content, NOT original TeX source spans.
    Do not feed these offsets to an A1 source map after normalization.
    """
    # Validate even an empty tree/mapping, using the existing B3 mapping contract.
    remap_citations('', mapping, arxiv_id=hierarchy.arxiv_id, version=hierarchy.version)
    if hierarchy.status not in {'ready', 'partial', 'unparseable'}:
        raise ValueError('Unknown hierarchy status')
    copied, audits, seen = deepcopy(hierarchy), [], set()
    if copied.root is None:
        if hierarchy.status != 'unparseable':
            raise ValueError('Ready/partial hierarchy requires a root')
        return RemappedHierarchy(copied, ())
    if hierarchy.status == 'unparseable':
        raise ValueError('Unparseable hierarchy must not expose a usable root')

    def visit(node):
        if node.occurrence_id in seen:
            raise ValueError('Duplicate version-local occurrence id')
        seen.add(node.occurrence_id)
        if node.children:
            children = [visit(child) for child in node.children]
            return _replace_fields(node, children=tuple(children) if isinstance(node.children, tuple) else children)
        if node.kind in {'document', 'heading', 'list', 'item'}:
            return node
        if not isinstance(node.content, str):
            raise ValueError('Leaf content is not finalized; call B1 before B3')
        result = remap_citations(node.content, mapping, arxiv_id=hierarchy.arxiv_id,
                                 version=hierarchy.version)
        audits.append(NodeCitationRemap(node.occurrence_id, node.kind, result))
        return _replace_fields(node, content=result.content)

    copied = _replace_fields(copied, root=visit(copied.root))
    return RemappedHierarchy(copied, tuple(audits))
