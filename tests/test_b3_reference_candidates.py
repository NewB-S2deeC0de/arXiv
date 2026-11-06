"""Review-only grouping tests; no expected canonical keys or merged records."""
from copy import deepcopy
from dataclasses import asdict, replace, FrozenInstanceError
import json
from pathlib import Path
import random
import unittest

from standardization import (extract_bibliography, SourceContext, extract_bibitem_metadata,
    prepare_c_bibentry, make_reference_occurrence, analyze_reference_candidates,
    ReferenceOccurrenceId, RawSourceMap, SourceMapPiece, MappedSourceSpan)


def occurrence(key='a', doi='10.1234/shared', *, pub='p', version=1, identity=None,
               metadata=None, body=None):
    if body is None:
        body = 'Body.' + (r'\url{https://doi.org/' + doi + '}' if doi else '')
    raw = r'\begin{thebibliography}{1}\bibitem{' + key + '}' + body + r'\end{thebibliography}'
    entry = extract_bibliography(raw, source=SourceContext(pub, version, 'refs.tex')).entries[0]
    prepared = prepare_c_bibentry(entry, metadata=metadata)
    return make_reference_occurrence(prepared, occurrence_id=identity or key or 'invalid-entry')


class ReferenceCandidateTests(unittest.TestCase):
    def analyze(self, records):
        records = tuple(records)
        before = deepcopy(records)
        result = analyze_reference_candidates(records)
        self.assertEqual(records, before)
        self.assertEqual(result, analyze_reference_candidates(reversed(records)))
        self.assertEqual(len(result.records), len(records))
        refs = {o.ref for o in records}
        grouped = {r for g in result.groups for r in g.members}
        self.assertEqual(grouped | set(result.ungrouped), refs)
        self.assertFalse(grouped & set(result.ungrouped))
        for group in result.groups:
            self.assertGreaterEqual(len(group.members), 2)
            self.assertTrue(all(r.arxiv_id == group.arxiv_id for r in group.members))
            self.assertEqual(group.reason, 'exact_selected_doi')
        return result

    def test_empty_input(self):
        result = self.analyze([])
        self.assertEqual((result.records, result.groups, result.ungrouped), ((), (), ()))

    def test_same_doi_different_keys_across_versions_is_a_candidate(self):
        records = [occurrence('old'), occurrence('new', version=2)]
        result = self.analyze(records)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].members, tuple(o.ref for o in records))
        self.assertEqual(result.groups[0].doi, '10.1234/shared')
        self.assertFalse(result.groups[0].review_reasons)
        self.assertFalse(hasattr(result, 'canonical_mapping'))

    def test_singleton_clear_doi_remains_ungrouped(self):
        record = occurrence()
        result = self.analyze([record])
        self.assertTrue(result.records[0].eligible)
        self.assertEqual(result.ungrouped, (record.ref,))

    def test_same_doi_different_publications_never_grouped(self):
        result = self.analyze([occurrence(pub='p'), occurrence(pub='other')])
        self.assertFalse(result.groups)

    def test_same_key_different_doi_across_versions_is_key_reuse_not_merge(self):
        result = self.analyze([occurrence(doi='10.1234/one'),
                               occurrence(doi='10.1234/two', version=2)])
        self.assertFalse(result.groups)
        self.assertEqual(len(result.key_findings), 1)
        finding = result.key_findings[0]
        self.assertEqual(finding.kind, 'key_reused_across_versions_with_different_doi')
        self.assertEqual(finding.selected_dois, ('10.1234/one', '10.1234/two'))

    def test_same_version_same_key_different_doi_is_collision(self):
        result = self.analyze([occurrence(identity='first', doi='10.1234/one'),
                               occurrence(identity='second', doi='10.1234/two')])
        self.assertFalse(result.groups)
        self.assertEqual(result.key_findings[0].kind, 'duplicate_key_same_version')

    def test_same_version_same_key_equal_doi_still_reports_key_collision(self):
        result = self.analyze([occurrence(identity='first'), occurrence(identity='second')])
        self.assertEqual(len(result.groups), 1)
        self.assertIn('same_version_key_collision', result.groups[0].review_reasons)
        self.assertEqual(len(result.records), 2)

    def test_same_version_different_keys_may_be_reviewed_without_merging(self):
        result = self.analyze([occurrence('a'), occurrence('b')])
        self.assertEqual(len(result.groups), 1)
        self.assertFalse(result.key_findings)

    def test_multiple_doi_entry_is_blocked(self):
        record = occurrence(body='10.1234/one 10.1234/two')
        result = self.analyze([record])
        self.assertFalse(result.records[0].eligible)
        self.assertEqual(result.records[0].doi_candidates, ('10.1234/one', '10.1234/two'))
        self.assertIn('multiple_doi_candidates', result.records[0].blockers)
        self.assertIn('doi_evidence_ambiguous', result.records[0].blockers)

    def test_ambiguous_entry_cannot_bridge_two_doi_groups(self):
        records = [occurrence('a1', '10.1234/A'), occurrence('a2', '10.1234/A', version=2),
                   occurrence('b1', '10.1234/B'), occurrence('b2', '10.1234/B', version=2),
                   occurrence('bridge', body='10.1234/A 10.1234/B')]
        result = self.analyze(records)
        self.assertEqual([g.doi for g in result.groups], ['10.1234/A', '10.1234/B'])
        self.assertTrue(all(len(g.members) == 2 for g in result.groups))
        self.assertIn(records[-1].ref, result.ungrouped)

    def test_missing_doi_does_not_group_by_same_title_author_year_or_raw(self):
        metadata = dict(title='Same title', authors=['A. Author'], year=2024)
        records = [occurrence('a', None, metadata=metadata),
                   occurrence('b', None, version=2, metadata=metadata)]
        result = self.analyze(records)
        self.assertFalse(result.groups)
        self.assertTrue(all('doi_missing' in a.blockers for a in result.records))

    def test_warned_punctuation_evidence_is_not_promoted(self):
        record = occurrence(body='DOI: 10.1234/shared.')
        result = self.analyze([record, occurrence('other', version=2)])
        self.assertFalse(result.groups)
        self.assertIn('doi_evidence_ambiguous', result.records[0].blockers)

    def test_conflicting_provided_and_raw_doi_is_preserved(self):
        record = occurrence(metadata={'doi': '10.9999/other'})
        result = self.analyze([record])
        self.assertFalse(result.records[0].eligible)
        self.assertIn('10.9999/other', result.records[0].doi_candidates)

    def test_unrecognized_provided_doi_is_blocked(self):
        record = occurrence('a', None, metadata={'doi': 'not-a-doi'})
        result = self.analyze([record])
        self.assertIn('doi_literal_outside_supported_grammar', result.records[0].blockers)

    def test_invalid_key_is_not_lost_or_given_a_canonical_key(self):
        record = occurrence('', identity='bad')
        result = self.analyze([record, occurrence('valid', version=2)])
        assessment = next(a for a in result.records if a.occurrence.ref == record.ref)
        self.assertIn('invalid_citation_key', assessment.blockers)
        self.assertIsNone(assessment.citation_key)
        self.assertIn('https://doi.org/', assessment.occurrence.prepared.entry.raw_content)

    def test_complementary_fields_reported_without_selecting_merged_values(self):
        records = [occurrence(metadata={'title': 'Title', 'authors': ['A. Author']}),
                   occurrence('b', version=2, metadata={'year': 2024, 'journal': 'Journal'})]
        group = self.analyze(records).groups[0]
        self.assertEqual(set(group.complementary_fields), {'title', 'authors', 'year', 'journal'})
        self.assertFalse(group.field_variations)
        self.assertFalse(hasattr(group, 'merged_entry'))

    def test_conflicting_year_and_title_values_keep_all_occurrence_evidence(self):
        records = [occurrence(metadata={'title': 'First', 'year': 2023}),
                   occurrence('b', version=2, metadata={'title': 'Second', 'year': 2024})]
        group = self.analyze(records).groups[0]
        variations = {v.field: v for v in group.field_variations}
        self.assertEqual(set(variations), {'title', 'year'})
        self.assertEqual({v.value for v in variations['year'].values}, {2023, 2024})
        self.assertEqual({r for v in variations['year'].values for r in v.occurrences}, {o.ref for o in records})
        self.assertIn('literal_metadata_values_differ', group.review_reasons)

    def test_authors_order_and_explicit_empty_list_are_not_silently_unioned(self):
        records = [occurrence(metadata={'authors': ['A. Author', 'B. Author']}),
                   occurrence('b', version=2, metadata={'authors': ['B. Author', 'A. Author']}),
                   occurrence('c', version=3, metadata={'authors': []})]
        variation = self.analyze(records).groups[0].field_variations[0]
        self.assertEqual(variation.field, 'authors')
        self.assertEqual(len(variation.values), 3)

    def test_unknown_c_defaults_do_not_create_false_field_conflicts(self):
        records = [occurrence(), occurrence('b', version=2, metadata={'entry_type': 'article', 'authors': ['A. Author']})]
        group = self.analyze(records).groups[0]
        self.assertFalse(group.field_variations)
        self.assertIn('entry_type', group.complementary_fields)
        self.assertIn('authors', group.complementary_fields)

    def test_exact_literal_policy_does_not_silently_normalize_doi_case(self):
        result = self.analyze([occurrence(doi='10.1234/ABC'), occurrence('b', '10.1234/abc', version=2)])
        self.assertFalse(result.groups)

    def test_same_identity_is_rejected_but_id_can_repeat_in_another_version(self):
        record = occurrence()
        with self.assertRaises(ValueError):
            analyze_reference_candidates([record, record])
        self.assertEqual(len(self.analyze([record, occurrence(version=2)]).records), 2)

    def test_publication_version_and_metadata_provenance_must_agree(self):
        original = occurrence()
        for overrides in ({'arxiv_id': 'other'}, {'version': 2}, {'occurrence_id': ' '}):
            args = dict(occurrence_id='a'); args.update(overrides)
            with self.assertRaises(ValueError):
                make_reference_occurrence(original.prepared, **args)
        with self.assertRaises(ValueError):
            analyze_reference_candidates([replace(original, ref=ReferenceOccurrenceId('p', True, 'a'))])
        other = occurrence('b')
        with self.assertRaises(ValueError):
            make_reference_occurrence(original.prepared, occurrence_id='a',
                                      metadata_result=extract_bibitem_metadata(other.prepared.entry))

    def test_generator_input_and_stable_group_identity(self):
        records = [occurrence(), occurrence('b', version=2)]
        result = self.analyze(records)
        self.assertEqual(result, analyze_reference_candidates(o for o in records))
        another = self.analyze([occurrence('c', version=3), *records])
        self.assertEqual(result.groups[0].candidate_group_id, another.groups[0].candidate_group_id)

    def test_optional_exact_entry_provenance_and_stale_map_rejection(self):
        raw = r'\begin{thebibliography}{1}\bibitem{k}10.1234/paper\end{thebibliography}'
        item = extract_bibliography(raw, source=SourceContext('p', 1)).entries[0]
        prepared = prepare_c_bibentry(item)
        boundary = item.content_start + 2
        mapping = RawSourceMap(raw, (
            SourceMapPiece(0, boundary, MappedSourceSpan('a.tex', 10, 10 + boundary)),
            SourceMapPiece(boundary, len(raw), MappedSourceSpan('b.tex', 0, len(raw) - boundary))))
        record = make_reference_occurrence(prepared, occurrence_id='k', source_map=mapping)
        self.assertEqual(record.source_mapping, 'exact_segments')
        self.assertEqual(record.source_spans, (
            MappedSourceSpan('a.tex', 10 + item.start, 10 + boundary),
            MappedSourceSpan('b.tex', 0, item.end - boundary)))
        changed = raw.replace('paper', 'other')
        bad_map = RawSourceMap(changed, (SourceMapPiece(0, len(changed), MappedSourceSpan('a', 0, len(changed))),))
        with self.assertRaises(ValueError):
            make_reference_occurrence(prepared, occurrence_id='k', source_map=bad_map)

    def test_results_are_frozen_and_json_serializable(self):
        result = self.analyze([occurrence(), occurrence('b', version=2)])
        with self.assertRaises(FrozenInstanceError):
            result.policy = 'changed'
        decoded = json.loads(json.dumps(asdict(result)))
        self.assertEqual(len(decoded['records']), 2)


    def test_seeded_input_permutations_do_not_change_report(self):
        records = [occurrence('a', metadata={'year': 2023}), occurrence('b', version=2, metadata={'year': 2024}),
                   occurrence('c', '10.9999/other'), occurrence('d', '10.9999/other', version=2),
                   occurrence('missing', None), occurrence('ambiguous', body='10.1234/shared 10.9999/other')]
        expected = self.analyze(records)
        rng = random.Random(20261104)
        for _ in range(100):
            shuffled = rng.sample(records, len(records))
            self.assertEqual(analyze_reference_candidates(shuffled), expected)


if __name__ == '__main__':
    unittest.main()
