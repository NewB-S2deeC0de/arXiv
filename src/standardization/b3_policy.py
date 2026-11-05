"""Automatic B3 policy: exact supported DOI identity, deterministic final keys.

Fixed-input determinism only. No registry, fuzzy auto-merge, network or implicit
resource selection. Unknown identities remain separate and evidence is retained.
"""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path

from .c_bibentry_adapter import CBibEntryPayload
from .citations import _KEY, remap_citations
from .normalize import normalize_text
from .parser_adapter import _replace_fields
from .reference_candidates import ReferenceOccurrenceId, analyze_reference_candidates
from .scanner import scan_latex
from .version_workflow import default_structural_content, to_c_tree


POLICY_ID = 'b3_exact_doi_fixed_input_v1'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class MergeFieldValue:
    value: object
    occurrences: tuple


@dataclass(frozen=True)
class MergeFieldConflict:
    field: str
    values: tuple[MergeFieldValue, ...]


@dataclass(frozen=True)
class MergedReference:
    payload: CBibEntryPayload
    members: tuple
    unknown_fields: tuple[str, ...]
    conflicts: tuple[MergeFieldConflict, ...]


@dataclass(frozen=True)
class MergeMappingConflict:
    arxiv_id: str
    version: int
    old_key: str
    requested_targets: tuple[str, ...]
    occurrences: tuple


@dataclass(frozen=True)
class ReferenceMergeResult:
    records: tuple[MergedReference, ...]
    mapping: dict
    mapping_conflicts: tuple[MergeMappingConflict, ...]
    unresolved: tuple
    occurrences: tuple
    policy: str = 'explicit_assignments_exact_field_union_v1'


def merge_reference_decisions(occurrences, assignments):
    """Merge explicit occurrence -> final key assignments, scoped by publication.

    Assignments are decisions supplied by caller/reviewer, not auto-generated
    by this API. Distinct/ambiguous known DOI evidence blocks a merge. Other
    field conflicts yield unknown projected values plus all evidence in audit.
    Ambiguous same-version old keys map to None, preserving citations downstream.
    """
    occurrences = tuple(occurrences)
    reviewed = analyze_reference_candidates(occurrences)  # validates identities/provenance
    if not isinstance(assignments, Mapping):
        raise TypeError('assignments must map occurrence identities to final citation keys')
    assignments = dict(assignments)
    indexed = {o.ref: o for o in occurrences}
    if set(assignments) - set(indexed):
        raise ValueError('Assignment refers to an unknown occurrence')
    for ref, key in assignments.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key == '*':
            raise ValueError('Assigned canonical key must be a valid literal citation key')
        if indexed[ref].prepared.payload is None:
            raise ValueError('Invalid original key must be resolved before assigning a final record')
    grouped = defaultdict(list)
    for ref, key in sorted(assignments.items()):
        grouped[ref.arxiv_id, key].append(indexed[ref])
    assessments = {a.occurrence.ref: a for a in reviewed.records}
    records = []
    fields = ('entry_type', 'title', 'authors', 'year', 'doi', 'journal', 'url')
    for (pub, key), members in sorted(grouped.items()):
        if len(members) > 1:
            doi_values = {doi for o in members for doi in assessments[o.ref].doi_candidates}
            if len(doi_values) > 1 or any(
                    'multiple_doi_candidates' in assessments[o.ref].blockers or
                    'doi_evidence_ambiguous' in assessments[o.ref].blockers for o in members):
                raise ValueError('Conflicting/ambiguous DOI evidence cannot be merged without adjudication')
        merged, unknown, conflicts = {}, [], []
        for field in fields:
            values = {}
            for occurrence in members:
                prepared = occurrence.prepared
                value = getattr(prepared.payload, field)
                if field in prepared.unknown_fields or value is None:
                    continue
                signature = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                if signature not in values:
                    values[signature] = [value, []]
                values[signature][1].append(occurrence.ref)
            if len(values) == 1:
                merged[field] = next(iter(values.values()))[0]
            else:
                merged[field] = None
                unknown.append(field)
                if values:
                    conflicts.append(MergeFieldConflict(field, tuple(
                        MergeFieldValue(value, tuple(refs)) for _, (value, refs) in sorted(values.items()))))
        payload = CBibEntryPayload(pub, key, merged['entry_type'] or 'misc',
            merged['title'], merged['authors'] or (), merged['year'], merged['doi'],
            merged['journal'], merged['url'])
        records.append(MergedReference(payload, tuple(o.ref for o in members),
                                       tuple(unknown), tuple(conflicts)))
    # Include unassigned occurrences in ambiguity checking. A duplicate key
    # cannot be remapped based only on the assigned subset of its occurrences.
    local = defaultdict(list)
    for occurrence in sorted(occurrences, key=lambda o: o.ref):
        key = occurrence.prepared.entry.citation_key
        if key is not None:
            local[occurrence.ref.arxiv_id, occurrence.ref.version, key].append(occurrence)
    mapping, mapping_conflicts = {}, []
    for scope, members in sorted(local.items()):
        targets = {assignments[o.ref] for o in members if o.ref in assignments}
        if not targets:
            continue
        if len(targets) != 1 or any(o.ref not in assignments for o in members):
            mapping[scope] = None
            mapping_conflicts.append(MergeMappingConflict(*scope, tuple(sorted(targets)),
                                                         tuple(o.ref for o in members)))
        else:
            mapping[scope] = next(iter(targets))
    # Reserve every final key against old-key collisions, even in versions
    # where that canonical key did not occur in the original bibliography.
    for record in records:
        pub, canonical = record.payload.arxiv_id, record.payload.citation_key
        for scope, target in mapping.items():
            if scope[0] == pub and scope[2] == canonical and target != canonical:
                raise ValueError('Canonical key collides with another old-key mapping; choose a fresh final key')
        if any(o.ref.arxiv_id == pub and o.prepared.entry.citation_key == canonical and
               assignments.get(o.ref) != canonical for o in occurrences):
            raise ValueError('Canonical key collides with an unresolved original key')
    for pub in {o.ref.arxiv_id for o in occurrences}:
        for version in {o.ref.version for o in occurrences if o.ref.arxiv_id == pub}:
            remap_citations('', mapping, arxiv_id=pub, version=version)
    unresolved = tuple(o.ref for o in sorted(occurrences, key=lambda o: o.ref) if o.ref not in assignments)
    return ReferenceMergeResult(tuple(records), mapping, tuple(mapping_conflicts), unresolved,
                                tuple(sorted(occurrences, key=lambda o: o.ref)))


@dataclass(frozen=True)
class AutomaticKeyDecision:
    occurrence: ReferenceOccurrenceId
    canonical_key: str | None
    reason: str


@dataclass(frozen=True)
class MetadataReviewCandidate:
    candidate_id: str
    arxiv_id: str
    normalized_title: str
    normalized_authors: tuple[str, ...]
    year: int
    members: tuple[ReferenceOccurrenceId, ...]
    known_dois: tuple[str, ...]
    blocked_by_doi_conflict: bool


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    message: str
    members: tuple[ReferenceOccurrenceId, ...]


@dataclass(frozen=True)
class AutomaticReferenceResult:
    policy_id: str
    input_fingerprint: str
    status: str
    merge: ReferenceMergeResult
    decisions: tuple[AutomaticKeyDecision, ...]
    metadata_candidates: tuple[MetadataReviewCandidate, ...]
    findings: tuple[PolicyFinding, ...]
    approved_metadata_candidates: tuple[str, ...] = ()


def _metadata_signature(assessment):
    """Conservative review-only fingerprint; never a merge identity."""
    prepared = assessment.occurrence.prepared
    if prepared.payload is None or set(assessment.blockers) - {'doi_missing'}:
        return None
    if any(name in prepared.unknown_fields for name in ('title', 'authors', 'year')):
        return None
    payload = prepared.payload
    if not payload.title or not payload.authors or type(payload.year) is not int:
        return None
    title = normalize_text(payload.title)
    # Avoid case-folding mathematics or interpreting custom title macros.
    if title.warnings or any(s.kind != 'text' for s in scan_latex(title.content).segments):
        return None
    normalized_title = ' '.join(title.content.split()).casefold()
    authors = tuple(' '.join(a.split()).casefold() for a in payload.authors)
    if not normalized_title or not all(authors):
        return None
    return assessment.occurrence.ref.arxiv_id, normalized_title, authors, payload.year


def unify_references(occurrences, *, approved_metadata_candidates=()):
    """Auto-unify same-DOI references; preserve all other valid occurrences.

    Non-DOI entries are singleton final records, even when metadata matches.
    Such matches become review candidates and may be approved by candidate ID.
    Ambiguous old keys map to None;
    invalid original keys stay unresolved without a projected row.
    """
    occurrences = tuple(occurrences)
    if isinstance(approved_metadata_candidates, str):
        raise TypeError('Expected an iterable of candidate IDs')
    approvals = set(approved_metadata_candidates)
    review = analyze_reference_candidates(occurrences)
    ordered = tuple(a.occurrence for a in review.records)
    fingerprint = _digest([asdict(o) for o in ordered])
    buckets = defaultdict(list)
    metadata = defaultdict(list)
    reserved = defaultdict(set)
    decisions = []
    findings = []
    for assessment in review.records:
        occurrence = assessment.occurrence
        ref = occurrence.ref
        if assessment.citation_key is not None:
            reserved[ref.arxiv_id].add(assessment.citation_key)
        if occurrence.prepared.payload is None:
            decisions.append(AutomaticKeyDecision(ref, None, 'invalid_key_unresolved'))
            findings.append(PolicyFinding('invalid_key_unresolved',
                'Raw occurrence retained; original key is not exportable.', (ref,)))
            continue
        identity = (['doi', assessment.selected_doi] if assessment.eligible else
                    ['occurrence', ref.version, ref.occurrence_id, _digest(asdict(occurrence))])
        buckets[ref.arxiv_id, _json(identity)].append(assessment)
        signature = _metadata_signature(assessment)
        if signature is not None:
            metadata[signature].append(assessment)
        if set(assessment.blockers) - {'doi_missing'}:
            findings.append(PolicyFinding('doi_identity_unresolved',
                'Ambiguous/unsupported DOI evidence retained in a separate record.', (ref,)))
    assignments = {}
    allocated = defaultdict(set)
    for (publication, identity), members in sorted(buckets.items()):
        base = 'ref_' + _digest([POLICY_ID, publication, json.loads(identity)])
        key, suffix = base, 0
        while key in reserved[publication] or key in allocated[publication]:
            suffix += 1
            key = base + '_' + str(suffix)
        allocated[publication].add(key)
        reason = ('exact_doi_merge' if len(members) > 1 else
                  'exact_doi_singleton' if members[0].eligible else 'separate_occurrence')
        for member in members:
            assignments[member.occurrence.ref] = key
            decisions.append(AutomaticKeyDecision(member.occurrence.ref, key, reason))
    candidates = []
    applied = set()
    for (publication, title, authors, year), members in sorted(metadata.items()):
        if len(members) < 2 or not any(a.selected_doi is None for a in members):
            continue
        dois = tuple(sorted({a.selected_doi for a in members if a.selected_doi is not None}))
        refs = tuple(a.occurrence.ref for a in members)
        candidate_id = 'metadata_review_' + _digest([
            publication, title, authors, year, [asdict(a.occurrence) for a in members]])
        candidates.append(MetadataReviewCandidate(candidate_id, publication, title, authors,
                                                  year, refs, dois, len(dois) > 1))
        if candidate_id in approvals:
            if len(dois) > 1:
                raise ValueError(f'Candidate has conflicting DOIs: {candidate_id}')
            doi_keys = [assignments[a.occurrence.ref] for a in members if a.selected_doi is not None]
            target = min(doi_keys or [assignments[ref] for ref in refs])
            assignments.update((ref, target) for ref in refs)
            applied.add(candidate_id)
            continue
        findings.append(PolicyFinding('metadata_match_review_only',
            'Title/authors/year match; a DOI is missing. Records remain separate.', refs))
    if approvals - applied:
        raise ValueError('Approval refers to an unknown metadata candidate')
    if applied:
        fingerprint = _digest([fingerprint, sorted(applied)])
        decisions = [AutomaticKeyDecision(d.occurrence, assignments[d.occurrence], 'metadata_review_approved')
                     if d.canonical_key is not None and assignments[d.occurrence] != d.canonical_key else d
                     for d in decisions]
    merged = merge_reference_decisions(ordered, assignments)
    for record in merged.records:
        if record.conflicts:
            findings.append(PolicyFinding('merged_metadata_conflict',
                'Known DOI identity merged; conflicting fields remain unknown with evidence.', record.members))
    for conflict in merged.mapping_conflicts:
        findings.append(PolicyFinding('ambiguous_old_key',
            'Same-version key has different or unresolved targets; citations must be retained.', conflict.occurrences))
    return AutomaticReferenceResult(POLICY_ID, fingerprint,
        'needs_review' if findings else 'ready', merged,
        tuple(sorted(decisions, key=lambda d: d.occurrence)), tuple(candidates),
        tuple(sorted(findings, key=lambda f: (f.code, f.members))), tuple(sorted(applied)))


def mapping_for_version(result, *, arxiv_id, version):
    """Explicit scoped view for C adapters; never collapse multiple versions."""
    if not isinstance(result, AutomaticReferenceResult):
        raise TypeError('result must be AutomaticReferenceResult')
    if not isinstance(arxiv_id, str) or not arxiv_id or type(version) is not int or version < 1:
        raise ValueError('Explicit publication and positive version required')
    return {old: target for (pub, ver, old), target in result.merge.mapping.items()
            if pub == arxiv_id and ver == version}


@dataclass(frozen=True)
class ProjectedCitationRemap:
    child_path: tuple[int, ...]
    kind: str
    result: object
    offset_basis: str = 'projected_node_content_before_remap'


@dataclass(frozen=True)
class FinalizedReferenceTree:
    tree: object
    nodes: tuple[ProjectedCitationRemap, ...]


def finalize_b3_tree(hierarchy, result, *, node_factory, structural_content=default_structural_content):
    """Project with caller's structural policy, then scoped-remap ALL contents.

    Includes headings/containers populated by a C projection callback, which
    leaf-only rich-tree remap cannot see. Already-canonical keys are identities;
    audit positions refer to projected content, never raw TeX spans.
    """
    view = mapping_for_version(result, arxiv_id=hierarchy.arxiv_id, version=hierarchy.version)
    mapping = {(hierarchy.arxiv_id, hierarchy.version, key): target for key, target in view.items()}
    for record in result.merge.records:
        if record.payload.arxiv_id == hierarchy.arxiv_id:
            mapping[hierarchy.arxiv_id, hierarchy.version, record.payload.citation_key] = record.payload.citation_key
    tree = to_c_tree(hierarchy, node_factory=node_factory, structural_content=structural_content)
    if tree is None:
        return FinalizedReferenceTree(None, ())
    audits = []

    def visit(node, path):
        remapped = remap_citations(node.content, mapping,
            arxiv_id=hierarchy.arxiv_id, version=hierarchy.version)
        audits.append(ProjectedCitationRemap(path, node.kind, remapped))
        children = [visit(child, path + (index,)) for index, child in enumerate(node.children)]
        return _replace_fields(node, content=remapped.content,
            children=tuple(children) if isinstance(node.children, tuple) else children)

    return FinalizedReferenceTree(visit(tree, ()), tuple(audits))


def b3_cache_fingerprint(*, parser_fingerprint: str | None = None):
    """Hash B code and policy, optionally composing with a parser service hash."""
    root = Path(__file__).parent
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))}
    payload = {'policy_id': POLICY_ID, 'standardization_code': code}
    if parser_fingerprint is not None:
        payload['parser_fingerprint'] = parser_fingerprint
    return _digest(payload)


def b3_audit_payload(result):
    """JSON-safe full B3 sidecar, including raw evidence and scoped mapping rows."""
    if not isinstance(result, AutomaticReferenceResult):
        raise TypeError('result must be AutomaticReferenceResult')
    body = asdict(result)
    body['merge']['mapping'] = [dict(arxiv_id=pub, version=ver, old_key=old,
                                    canonical_key=target)
        for (pub, ver, old), target in sorted(result.merge.mapping.items())]
    return {'format_version': 1, 'cache_fingerprint': b3_cache_fingerprint(), 'result': body}


def write_b3_audit(result, path):
    """Write a new sidecar, refusing overwrite. C owns transactional export.

    A sidecar alone is not a registry/checkpoint or atomic multi-table commit.
    """
    payload = _json(b3_audit_payload(result)) + '\n'
    path = Path(path)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path
