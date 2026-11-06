"""Public API for Milestone 2 standardization and reference reconciliation."""

import sys as _sys

from .bibliography import (BibliographyTarget, BibliographyDeclaration,
    BibliographyDependencies, extract_bibliography, inspect_bibliography_dependencies)
from .models import (BibItem, BibliographyRegion, BibliographyResult, RawFragment, SourceContext,
    Diagnostic, NormalizationResult, ScanResult, Segment)
from .normalize import normalize_block_math, normalize_figure, normalize_inline_math, normalize_text
from .scanner import scan_latex
from .reference_links import extract_reference_links, LinkEvidence, ReferenceLinksResult, normalize_doi_literal
from .parser_adapter import (normalize_parser_leaf, extract_parser_bibliography,
                             BoundDiagnostic, ParserNormalizationResult, ParserBibliographyResult,
                             GatheredBibliographyResult, extract_gathered_bibliography,
                             NormalizedA3Hierarchy, normalize_a3_hierarchy,
                             VersionBibliographyInspection, inspect_version_bibliography)
from .source_mapping import (MappedSourceSpan, SourceMapPiece, RawSourceMap,
                             source_map_from_gathered, source_map_from_spans)
from .c_bibentry_adapter import (CBibEntryPayload, PreparedCBibEntry, PreparedBibtexEntry,
                                 prepare_bibtex_entry, prepare_c_bibentry, to_c_bibentry)
from .bibitem_metadata import BibFieldEvidence, BibMetadataResult, extract_bibitem_metadata
from .citations import (CitationArgument, CitationKey, Citation, CitationScanResult,
                        CitationEdit, CitationKeyDecision, CitationRemapResult,
                        scan_citations, remap_citations)
from .reference_candidates import (ReferenceOccurrenceId, ReferenceOccurrence,
    ReferenceAssessment, CandidateFieldValue, CandidateFieldVariation, CandidateMissingField,
    ReferenceCandidateGroup, CitationKeyReuse, ReferenceCandidateResult,
    make_reference_occurrence, analyze_reference_candidates)
from .bibtex_reader import (BibtexField, BibtexEntry, BibtexFragment, BibtexReadResult, read_bibtex)
from .version_workflow import (BibResourceRead, PreparedVersionBibliography, prepare_version_bibliography,
    to_c_tree, NodeCitationRemap, RemappedHierarchy, remap_a3_hierarchy)
from .b3_policy import (POLICY_ID, AutomaticKeyDecision, MetadataReviewCandidate, PolicyFinding,
    AutomaticReferenceResult, unify_references, mapping_for_version, b3_cache_fingerprint,
    b3_audit_payload, write_b3_audit, ProjectedCitationRemap, FinalizedReferenceTree, finalize_b3_tree,
    MergeFieldValue, MergeFieldConflict, MergedReference, MergeMappingConflict,
    ReferenceMergeResult, merge_reference_decisions)
from .publication import (PublicationVersionInput, StandardizedPublication, StandardizedVersion,
                          standardize_publication)

# Preserve direct imports of the consolidated modules for existing adapters.
for _old, _target in {
    'bibliography_models': 'models',
    'bibliography_dependencies': 'bibliography',
    'figure_cleanup': 'normalize',
    'bibtex_projection': 'c_bibentry_adapter',
    'reference_merge': 'b3_policy',
}.items():
    _module = _sys.modules[f'{__name__}.{_target}']
    _sys.modules[f'{__name__}.{_old}'] = _module
    globals()[_old] = _module
del _old, _target, _module, _sys

__all__ = [
    'PublicationVersionInput', 'StandardizedPublication', 'StandardizedVersion',
    'standardize_publication',
    'POLICY_ID', 'AutomaticKeyDecision', 'MetadataReviewCandidate', 'PolicyFinding',
    'AutomaticReferenceResult', 'unify_references', 'mapping_for_version',
    'b3_cache_fingerprint', 'b3_audit_payload', 'write_b3_audit', 'normalize_doi_literal',
    'ProjectedCitationRemap', 'FinalizedReferenceTree', 'finalize_b3_tree',
    'BibtexField', 'BibtexEntry', 'BibtexFragment', 'BibtexReadResult', 'read_bibtex',
    'PreparedBibtexEntry', 'prepare_bibtex_entry',
    'BibResourceRead', 'PreparedVersionBibliography', 'prepare_version_bibliography', 'to_c_tree',
    'NodeCitationRemap', 'RemappedHierarchy', 'remap_a3_hierarchy',
    'MergeFieldValue', 'MergeFieldConflict', 'MergedReference', 'MergeMappingConflict',
    'ReferenceMergeResult', 'merge_reference_decisions',
    'Diagnostic', 'NormalizationResult', 'ScanResult', 'Segment', 'scan_latex',
    'normalize_text', 'normalize_inline_math', 'normalize_block_math',
    'extract_bibliography', 'BibItem', 'BibliographyRegion', 'BibliographyResult',
    'RawFragment', 'SourceContext',
    'extract_reference_links', 'LinkEvidence', 'ReferenceLinksResult',
    'normalize_parser_leaf', 'extract_parser_bibliography', 'BoundDiagnostic',
    'ParserNormalizationResult', 'ParserBibliographyResult',
    'GatheredBibliographyResult', 'extract_gathered_bibliography',
    'NormalizedA3Hierarchy', 'normalize_a3_hierarchy', 'normalize_figure',
    'VersionBibliographyInspection', 'inspect_version_bibliography',
    'BibliographyTarget', 'BibliographyDeclaration', 'BibliographyDependencies',
    'inspect_bibliography_dependencies',
    'MappedSourceSpan', 'SourceMapPiece', 'RawSourceMap', 'source_map_from_gathered',
    'source_map_from_spans',
    'CBibEntryPayload', 'PreparedCBibEntry', 'prepare_c_bibentry', 'to_c_bibentry',
    'BibFieldEvidence', 'BibMetadataResult', 'extract_bibitem_metadata',
    'CitationArgument', 'CitationKey', 'Citation', 'CitationScanResult',
    'CitationEdit', 'CitationKeyDecision', 'CitationRemapResult',
    'scan_citations', 'remap_citations',
    'ReferenceOccurrenceId', 'ReferenceOccurrence', 'ReferenceAssessment',
    'CandidateFieldValue', 'CandidateFieldVariation', 'CandidateMissingField',
    'ReferenceCandidateGroup', 'CitationKeyReuse', 'ReferenceCandidateResult',
    'make_reference_occurrence', 'analyze_reference_candidates',
]
