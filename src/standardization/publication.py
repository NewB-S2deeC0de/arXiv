"""Publication-level handoff from A3 to C2."""

from dataclasses import dataclass
from pathlib import Path

from .b3_policy import AutomaticReferenceResult, FinalizedReferenceTree, finalize_b3_tree, unify_references
from .parser_adapter import NormalizedA3Hierarchy, normalize_a3_hierarchy
from .version_workflow import PreparedVersionBibliography, default_structural_content, prepare_version_bibliography

_INCOMPLETE_BIBTEX = {'bibtex_invalid_header', 'bibtex_unclosed_record'}


@dataclass(frozen=True)
class PublicationVersionInput:
    hierarchy: object
    gathered: object
    source_dir: Path
    source_texts: dict[str, str] | None = None
    selected_bib_files: tuple[str, ...] | None = None


@dataclass(frozen=True)
class StandardizedVersion:
    version: int
    status: str
    normalized: NormalizedA3Hierarchy
    bibliography: PreparedVersionBibliography
    finalized: FinalizedReferenceTree | None


@dataclass(frozen=True)
class StandardizedPublication:
    arxiv_id: str
    versions: tuple[StandardizedVersion, ...]
    references: AutomaticReferenceResult


def standardize_publication(arxiv_id, versions, *, node_factory, include_partial=False,
                            approved_metadata_candidates=(), structural_content=default_structural_content):
    """Normalize, collect and unify eligible versions before remapping for C2."""
    ordered = sorted(versions, key=lambda item: item.hierarchy.version)
    seen = set()
    staged = []
    occurrences = []
    for item in ordered:
        hierarchy = item.hierarchy
        version = hierarchy.version
        if (hierarchy.arxiv_id != arxiv_id or type(version) is not int or
                version < 1 or version in seen):
            raise ValueError('Expected distinct positive versions of the requested publication')
        seen.add(version)
        bibliography = prepare_version_bibliography(hierarchy, item.gathered,
            source_texts=item.source_texts, source_dir=item.source_dir,
            selected_bib_files=() if hierarchy.status == 'unparseable' else item.selected_bib_files)
        normalized = normalize_a3_hierarchy(hierarchy, source_texts=item.source_texts)
        status = hierarchy.status
        if status == 'ready' and (bibliography.selection_diagnostics or any(
                resource.diagnostic is not None or any(
                    warning.code in _INCOMPLETE_BIBTEX for warning in resource.parsed.warnings)
                for resource in bibliography.resources)):
            status = 'partial'
        eligible = status == 'ready' or (include_partial and status == 'partial')
        if eligible:
            occurrences.extend(bibliography.occurrences)
        staged.append((version, status, normalized, bibliography, eligible))

    references = unify_references(occurrences, approved_metadata_candidates=approved_metadata_candidates)
    results = tuple(StandardizedVersion(version, status, normalized, bibliography,
        finalize_b3_tree(normalized.hierarchy, references, node_factory=node_factory,
                         structural_content=structural_content) if eligible else None)
        for version, status, normalized, bibliography, eligible in staged)
    return StandardizedPublication(arxiv_id, results, references)
