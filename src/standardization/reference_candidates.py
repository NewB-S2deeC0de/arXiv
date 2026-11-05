"""B3 review-only DOI candidate grouping; never merges records or emits a key map.

Uses exact selected DOI literals. No network, DOI canonicalization, fuzzy title
matching, transitive linkage through ambiguous records or metadata selection.
"""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json

from .bibitem_metadata import BibMetadataResult
from .c_bibentry_adapter import PreparedCBibEntry
from .c_bibentry_adapter import PreparedBibtexEntry, _doi_literal
from .reference_links import _DOI
from .source_mapping import RawSourceMap, MappedSourceSpan


@dataclass(frozen=True, order=True)
class ReferenceOccurrenceId:
    arxiv_id: str
    version: int
    occurrence_id: str


@dataclass(frozen=True)
class ReferenceOccurrence:
    ref: ReferenceOccurrenceId
    prepared: PreparedCBibEntry | PreparedBibtexEntry
    metadata_result: BibMetadataResult | None = None
    source_spans: tuple[MappedSourceSpan, ...] = ()
    source_mapping: str = 'entry_context_only'


@dataclass(frozen=True)
class ReferenceAssessment:
    occurrence: ReferenceOccurrence
    citation_key: str | None
    doi_candidates: tuple[str, ...]
    selected_doi: str | None
    eligible: bool
    blockers: tuple[str, ...]
    candidate_group_id: str | None


@dataclass(frozen=True)
class CandidateFieldValue:
    value: object
    occurrences: tuple[ReferenceOccurrenceId, ...]


@dataclass(frozen=True)
class CandidateFieldVariation:
    field: str
    values: tuple[CandidateFieldValue, ...]


@dataclass(frozen=True)
class CandidateMissingField:
    field: str
    occurrences: tuple[ReferenceOccurrenceId, ...]


@dataclass(frozen=True)
class ReferenceCandidateGroup:
    candidate_group_id: str
    arxiv_id: str
    doi: str
    members: tuple[ReferenceOccurrenceId, ...]
    reason: str
    field_variations: tuple[CandidateFieldVariation, ...]
    missing_fields: tuple[CandidateMissingField, ...]
    complementary_fields: tuple[str, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CitationKeyReuse:
    arxiv_id: str
    citation_key: str
    occurrences: tuple[ReferenceOccurrenceId, ...]
    selected_dois: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class ReferenceCandidateResult:
    records: tuple[ReferenceAssessment, ...]
    groups: tuple[ReferenceCandidateGroup, ...]
    key_findings: tuple[CitationKeyReuse, ...]
    ungrouped: tuple[ReferenceOccurrenceId, ...]
    policy: str = 'review_only_exact_selected_doi_v1'


def _validate_occurrence(occurrence):
    if not isinstance(occurrence, ReferenceOccurrence):
        raise TypeError('Expected ReferenceOccurrence records')
    ref, prepared = occurrence.ref, occurrence.prepared
    if (not isinstance(ref, ReferenceOccurrenceId) or not isinstance(ref.arxiv_id, str)
            or not ref.arxiv_id.strip() or type(ref.version) is not int or ref.version < 1
            or not isinstance(ref.occurrence_id, str) or not ref.occurrence_id.strip()):
        raise ValueError('Explicit publication, positive version and nonempty occurrence_id required')
    if not isinstance(prepared, (PreparedCBibEntry, PreparedBibtexEntry)):
        raise TypeError('prepared must be a B2 prepared bibitem or BibTeX entry')
    source = prepared.entry.source
    if source is not None:
        if source.arxiv_id is not None and source.arxiv_id != ref.arxiv_id:
            raise ValueError('Occurrence publication disagrees with B2 provenance')
        if source.version is not None and source.version != ref.version:
            raise ValueError('Occurrence version disagrees with B2 provenance')
    if prepared.payload is not None and prepared.payload.arxiv_id != ref.arxiv_id:
        raise ValueError('Occurrence publication disagrees with C payload')
    metadata = occurrence.metadata_result
    if metadata is not None and (not isinstance(metadata, BibMetadataResult)
                                 or metadata.entry != prepared.entry):
        raise ValueError('metadata_result must describe the same B2 entry')
    if occurrence.source_mapping not in {'entry_context_only', 'exact_segments'}:
        raise ValueError('Unknown provenance mapping status')
    if not isinstance(occurrence.source_spans, tuple) or any(
            not isinstance(span, MappedSourceSpan) for span in occurrence.source_spans):
        raise TypeError('source_spans must be a tuple of MappedSourceSpan')


def make_reference_occurrence(prepared, *, occurrence_id, arxiv_id=None, version=None,
                              metadata_result=None, source_map=None):
    """Assign a caller-owned occurrence id, retaining the entire B2 audit wrapper.

    Id uniqueness is per publication/version, not final C element identity.
    Source context is reused when present; explicit overrides must agree.
    """
    if not isinstance(prepared, (PreparedCBibEntry, PreparedBibtexEntry)):
        raise TypeError('prepared must be a B2 prepared bibitem or BibTeX entry')
    source = prepared.entry.source
    if arxiv_id is None:
        arxiv_id = source.arxiv_id if source is not None else None
        if arxiv_id is None and prepared.payload is not None:
            arxiv_id = prepared.payload.arxiv_id
    if version is None and source is not None:
        version = source.version
    origins, mapping_status = (), 'entry_context_only'
    if source_map is not None:
        if not isinstance(source_map, RawSourceMap):
            raise TypeError('source_map must describe the original B2 raw input')
        entry = prepared.entry
        origins = source_map.resolve(entry.start, entry.end)
        if (source_map.raw[entry.start:entry.end] != entry.raw or
                isinstance(prepared, PreparedCBibEntry) and
                source_map.raw[entry.content_start:entry.end] != entry.raw_content):
            raise ValueError('Source map does not match this B2 entry; exact provenance unavailable')
        mapping_status = 'exact_segments'
    occurrence = ReferenceOccurrence(ReferenceOccurrenceId(arxiv_id, version, occurrence_id),
                                     prepared, metadata_result, origins, mapping_status)
    _validate_occurrence(occurrence)
    return occurrence


def _assessment(occurrence):
    prepared = occurrence.prepared
    duplicate_doi = False
    if isinstance(prepared, PreparedBibtexEntry):
        doi_fields = [f for f in prepared.entry.fields if f.name == 'doi']
        candidates = {_doi_literal(f.value) or f.value.strip() for f in doi_fields
                      if f.value is not None and f.value.strip()}
        duplicate_doi = len(doi_fields) > 1
    else:
        candidates = {c.value for c in prepared.link_evidence if c.kind == 'doi'}
        provided = dict(prepared.provided_metadata).get('doi')
        if provided is not None:
            candidates.add(_doi_literal(provided) or provided.strip())
    selected = prepared.payload.doi if prepared.payload is not None else None
    if selected is not None:
        candidates.add(selected)
    blockers = []
    if prepared.entry.citation_key is None:
        blockers.append('invalid_citation_key')
    if len(candidates) > 1:
        blockers.append('multiple_doi_candidates')
    if duplicate_doi or any(w.code == 'ambiguous_doi' for w in prepared.warnings):
        blockers.append('doi_evidence_ambiguous')
    if any(not _DOI.fullmatch(value) for value in candidates):
        blockers.append('doi_literal_outside_supported_grammar')
    if selected is None or 'doi' in prepared.unknown_fields:
        blockers.append('doi_not_selected' if candidates else 'doi_missing')
    elif not _DOI.fullmatch(selected):
        blockers.append('doi_literal_outside_supported_grammar')
    return ReferenceAssessment(occurrence, prepared.entry.citation_key, tuple(sorted(candidates)),
                               selected, not blockers, tuple(sorted(set(blockers))), None)


def _group_id(publication, doi):
    key = json.dumps([publication, doi], ensure_ascii=False, separators=(',', ':'))
    return 'doi_candidate_' + hashlib.sha256(key.encode('utf-8')).hexdigest()


def analyze_reference_candidates(occurrences):
    """Report exact DOI groups and scoped key/metadata findings without merging.

    Unknown fields and C default placeholders never count as known metadata.
    Same author/year/title/raw alone does not create a candidate group.
    Ambiguous multi-DOI entries cannot bridge two otherwise distinct groups.
    """
    occurrences = tuple(occurrences)
    seen = set()
    for occurrence in occurrences:
        _validate_occurrence(occurrence)
        if occurrence.ref in seen:
            raise ValueError('Duplicate occurrence identity; do not collapse repeated bibliography entries')
        seen.add(occurrence.ref)
    ordered = sorted(occurrences, key=lambda o: o.ref)
    assessments = [_assessment(o) for o in ordered]
    doi_index, local_keys, publication_keys = defaultdict(list), defaultdict(list), defaultdict(list)
    for assessment in assessments:
        ref = assessment.occurrence.ref
        if assessment.eligible:
            doi_index[ref.arxiv_id, assessment.selected_doi].append(assessment)
        if assessment.citation_key is not None:
            local_keys[ref.arxiv_id, ref.version, assessment.citation_key].append(assessment)
            publication_keys[ref.arxiv_id, assessment.citation_key].append(assessment)
    key_findings, colliding_refs = [], set()
    for (pub, version, key), members in sorted(local_keys.items()):
        if len(members) > 1:
            refs = tuple(m.occurrence.ref for m in members)
            colliding_refs.update(refs)
            key_findings.append(CitationKeyReuse(pub, key, refs,
                tuple(sorted({m.selected_doi for m in members if m.selected_doi is not None})),
                'duplicate_key_same_version'))
    for (pub, key), members in sorted(publication_keys.items()):
        dois = {m.selected_doi for m in members if m.eligible}
        if len({m.occurrence.ref.version for m in members}) > 1 and len(dois) > 1:
            key_findings.append(CitationKeyReuse(pub, key, tuple(m.occurrence.ref for m in members),
                                                tuple(sorted(dois)), 'key_reused_across_versions_with_different_doi'))
    groups, memberships = [], {}
    fields = ('entry_type', 'title', 'authors', 'year', 'journal', 'url')
    for (pub, doi), members in sorted(doi_index.items()):
        if len(members) < 2:
            continue
        group_id = _group_id(pub, doi)
        refs = tuple(m.occurrence.ref for m in members)
        variations, missing, complementary = [], [], []
        for field in fields:
            values = {}
            absent = []
            for assessment in members:
                prepared = assessment.occurrence.prepared
                value = getattr(prepared.payload, field)
                if field in prepared.unknown_fields or value is None:
                    absent.append(assessment.occurrence.ref)
                else:
                    # JSON distinguishes scalar types and keeps author list order.
                    signature = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                    if signature not in values:
                        values[signature] = (value, [])
                    values[signature][1].append(assessment.occurrence.ref)
            if len(values) > 1:
                variations.append(CandidateFieldVariation(field, tuple(
                    CandidateFieldValue(value, tuple(refs)) for _, (value, refs) in sorted(values.items()))))
            if absent:
                missing.append(CandidateMissingField(field, tuple(absent)))
                if len(values) == 1:
                    complementary.append(field)
        reasons = []
        if variations:
            reasons.append('literal_metadata_values_differ')
        if any(ref in colliding_refs for ref in refs):
            reasons.append('same_version_key_collision')
        groups.append(ReferenceCandidateGroup(group_id, pub, doi, refs, 'exact_selected_doi',
            tuple(variations), tuple(missing), tuple(complementary), tuple(reasons)))
        memberships.update((ref, group_id) for ref in refs)
    final_records = tuple(ReferenceAssessment(a.occurrence, a.citation_key, a.doi_candidates,
        a.selected_doi, a.eligible, a.blockers, memberships.get(a.occurrence.ref)) for a in assessments)
    key_findings.sort(key=lambda k: (k.arxiv_id, k.citation_key, k.kind, k.occurrences))
    return ReferenceCandidateResult(final_records, tuple(groups), tuple(key_findings),
        tuple(a.occurrence.ref for a in assessments if a.occurrence.ref not in memberships))
