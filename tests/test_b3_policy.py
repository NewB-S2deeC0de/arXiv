"""Approved fixed-input B3 policy: identity, keys, conflicts and scoped audit."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import random
import re
import tempfile
import unittest
from types import SimpleNamespace

import test_b1_b2_b3_version_workflow as fixtures
from standardization import (unify_references, mapping_for_version, b3_cache_fingerprint,
    b3_audit_payload, write_b3_audit, normalize_doi_literal, prepare_c_bibentry,
    make_reference_occurrence, read_bibtex, prepare_bibtex_entry, SourceContext,
    remap_citations, analyze_reference_candidates, normalize_a3_hierarchy,
    remap_a3_hierarchy, finalize_b3_tree)


def occurrence(key='a', *, doi=None, version=1, identity=None, metadata=None, pub='p'):
    return fixtures.occurrence(key,doi=doi,version=version,identity=identity,metadata=metadata,pub=pub)


class AutomaticPolicyTests(unittest.TestCase):
    def test_same_doi_different_keys_auto_merges_and_unions_fields(self):
        a=occurrence('old',doi='10.1234/shared',metadata={'title':'Title','year':2020})
        b=occurrence('new',doi='10.1234/shared',version=2,metadata={'authors':['A. Author'],'journal':'J'})
        before=deepcopy((a,b))
        result=unify_references([b,a])
        self.assertEqual((a,b),before)
        self.assertEqual(result.status,'ready')
        self.assertEqual(len(result.merge.records),1)
        row=result.merge.records[0].payload
        self.assertEqual((row.title,row.year,row.authors,row.journal),('Title',2020,('A. Author',),'J'))
        self.assertRegex(row.citation_key,r'^ref_[0-9a-f]{64}$')
        self.assertEqual(set(result.merge.mapping.values()),{row.citation_key})
        self.assertEqual(result,unify_references([a,b]))

    def test_fixed_input_determinism_includes_audit_and_reordering(self):
        records=[occurrence('old',doi='10.1234/a'),occurrence('new',doi='10.1234/a',version=2),
                 occurrence('separate',version=3)]
        baseline=unify_references(records)
        randomizer=random.Random(20261105)
        for _ in range(8):
            randomizer.shuffle(records)
            self.assertEqual(unify_references(records),baseline)
            self.assertEqual(b3_audit_payload(unify_references(records)),b3_audit_payload(baseline))

    def test_different_doi_never_merges_despite_equal_metadata(self):
        metadata={'title':'Title','authors':['Author'],'year':2020}
        records=[occurrence('a',doi='10.1234/a',metadata=metadata),
                 occurrence('b',doi='10.1234/b',version=2,metadata=metadata)]
        result=unify_references(records)
        self.assertEqual(len(result.merge.records),2)
        self.assertFalse(result.metadata_candidates)

    def test_same_doi_metadata_conflict_is_unknown_with_all_evidence(self):
        a=occurrence('a',doi='10.1234/a',metadata={'year':2020,'title':'First'})
        b=occurrence('b',doi='10.1234/a',version=2,metadata={'year':2021,'title':'Second'})
        result=unify_references([a,b])
        self.assertEqual(result.status,'needs_review')
        record=result.merge.records[0]
        self.assertIsNone(record.payload.year)
        self.assertIsNone(record.payload.title)
        self.assertEqual({c.field for c in record.conflicts},{'year','title'})
        self.assertEqual(len(result.merge.occurrences),2)

    def test_no_doi_matching_metadata_is_review_only_not_auto_merge(self):
        a=occurrence('a',metadata={'title':r'A \textbf{Title}','authors':['A. Author'],'year':2020})
        b=occurrence('b',version=2,metadata={'title':'a title','authors':['a. author'],'year':2020})
        result=unify_references([a,b])
        self.assertEqual(len(result.merge.records),2)
        self.assertEqual(len(result.metadata_candidates),1)
        self.assertEqual(result.status,'needs_review')

    def test_one_missing_doi_matches_only_for_review(self):
        metadata={'title':'Title','authors':['Author'],'year':2020}
        a=occurrence('a',doi='10.1234/a',metadata=metadata)
        b=occurrence('b',version=2,metadata=metadata)
        result=unify_references([a,b])
        self.assertEqual(len(result.merge.records),2)
        self.assertEqual(result.metadata_candidates[0].known_dois,('10.1234/a',))

    def test_missing_doi_cannot_bridge_two_known_dois(self):
        metadata={'title':'Title','authors':['Author'],'year':2020}
        records=[occurrence('a',doi='10.1234/a',metadata=metadata),
                 occurrence('b',doi='10.1234/b',version=2,metadata=metadata),
                 occurrence('c',version=3,metadata=metadata)]
        result=unify_references(records)
        self.assertEqual(len(result.merge.records),3)
        self.assertTrue(result.metadata_candidates[0].blocked_by_doi_conflict)

    def test_author_order_and_missing_fields_do_not_create_metadata_candidates(self):
        records=[occurrence('a',metadata={'title':'Title','authors':['A','B'],'year':2020}),
                 occurrence('b',version=2,metadata={'title':'Title','authors':['B','A'],'year':2020}),
                 occurrence('c',version=3,metadata={'title':'Title','year':2020})]
        self.assertFalse(unify_references(records).metadata_candidates)

    def test_mathematical_title_is_not_casefolded_into_false_match(self):
        metadata={'authors':['A'],'year':2020}
        records=[occurrence('a',metadata={**metadata,'title':'Use $X$'}),
                 occurrence('b',version=2,metadata={**metadata,'title':'Use $x$'})]
        self.assertFalse(unify_references(records).metadata_candidates)

    def test_same_key_different_versions_has_independent_mapping(self):
        a=occurrence('same',doi='10.1234/a')
        b=occurrence('same',doi='10.1234/b',version=2)
        result=unify_references([a,b])
        one=mapping_for_version(result,arxiv_id='p',version=1)
        two=mapping_for_version(result,arxiv_id='p',version=2)
        self.assertNotEqual(one['same'],two['same'])
        self.assertEqual(remap_citations(r'\cite{same}',result.merge.mapping,arxiv_id='p',version=2).content,
                         r'\cite{'+two['same']+'}')

    def test_same_version_duplicate_key_distinct_identity_maps_none(self):
        a=occurrence('same',identity='first',doi='10.1234/a')
        b=occurrence('same',identity='second',doi='10.1234/b')
        result=unify_references([a,b])
        self.assertEqual(len(result.merge.records),2)
        self.assertIsNone(result.merge.mapping['p',1,'same'])
        self.assertIn('ambiguous_old_key',[f.code for f in result.findings])

    def test_same_version_duplicate_key_same_doi_can_resolve_to_one(self):
        a=occurrence('same',identity='first',doi='10.1234/a')
        b=occurrence('same',identity='second',doi='10.1234/a')
        result=unify_references([a,b])
        self.assertEqual(len(result.merge.records),1)
        self.assertIsNotNone(result.merge.mapping['p',1,'same'])
        self.assertEqual(result.status,'ready')

    def test_ambiguous_doi_is_not_auto_merged(self):
        entry=fixtures.extract_bibliography(r'\begin{thebibliography}{1}\bibitem{x}10.1234/a 10.1234/b\end{thebibliography}',
                                           source=SourceContext('p',1)).entries[0]
        ambiguous=make_reference_occurrence(prepare_c_bibentry(entry),occurrence_id='ambiguous')
        known=occurrence('known',doi='10.1234/a',version=2)
        result=unify_references([ambiguous,known])
        self.assertEqual(len(result.merge.records),2)
        self.assertIn('doi_identity_unresolved',[f.code for f in result.findings])

    def test_invalid_original_key_is_retained_without_final_row(self):
        entry=read_bibtex('@misc{bad key,title={Raw}}',source=SourceContext('p',1)).entries[0]
        invalid=make_reference_occurrence(prepare_bibtex_entry(entry),occurrence_id='invalid')
        result=unify_references([invalid])
        self.assertFalse(result.merge.records)
        self.assertEqual(result.merge.unresolved,(invalid.ref,))
        self.assertEqual(result.status,'needs_review')

    def test_no_doi_singletons_remain_exportable_not_dropped(self):
        result=unify_references([occurrence('a'),occurrence('b',version=2)])
        self.assertEqual(len(result.merge.records),2)
        self.assertFalse(result.merge.unresolved)
        self.assertEqual(len(result.merge.mapping),2)

    def test_canonical_allocator_avoids_original_key_collision(self):
        original=occurrence('a',doi='10.1234/a')
        initial=unify_references([original]).merge.records[0].payload.citation_key
        other=occurrence(initial,version=2)
        result=unify_references([other,original])
        target=result.merge.mapping['p',1,'a']
        self.assertEqual(target,initial+'_1')
        self.assertNotIn(initial,{r.payload.citation_key for r in result.merge.records})

    def test_publications_are_isolated_even_with_same_doi(self):
        result=unify_references([occurrence('a',doi='10.1234/a'),occurrence('a',doi='10.1234/a',pub='q')])
        self.assertEqual(len(result.merge.records),2)
        self.assertEqual(len({r.payload.citation_key for r in result.merge.records}),2)

    def test_empty_input_and_mapping_view_validation(self):
        result=unify_references([])
        self.assertEqual(result.status,'ready')
        self.assertFalse(result.merge.records)
        self.assertEqual(mapping_for_version(result,arxiv_id='p',version=1),{})
        for pub,version in [('',1),('p',True),('p',0)]:
            with self.assertRaises(ValueError):mapping_for_version(result,arxiv_id=pub,version=version)
        with self.assertRaises(TypeError):mapping_for_version(None,arxiv_id='p',version=1)


class CompatibilityAndAuditTests(unittest.TestCase):
    def test_doi_url_metadata_and_plain_raw_are_not_a_false_conflict(self):
        prepared=prepare_c_bibentry(occurrence('a',doi='10.1234/a').prepared.entry,
                                   metadata={'doi':' https://doi.org/10.1234/a '})
        self.assertEqual(prepared.payload.doi,'10.1234/a')
        self.assertEqual(dict(prepared.provided_metadata)['doi'],' https://doi.org/10.1234/a ')
        record=make_reference_occurrence(prepared,occurrence_id='a')
        self.assertTrue(analyze_reference_candidates([record]).records[0].eligible)

    def test_invalid_provided_doi_cannot_be_exported_as_known(self):
        prepared=prepare_c_bibentry(occurrence('a').prepared.entry,metadata={'doi':'not-a-doi'})
        self.assertIsNone(prepared.payload.doi)
        self.assertIn('doi',prepared.unknown_fields)
        self.assertEqual(dict(prepared.provided_metadata)['doi'],'not-a-doi')

    def test_supported_doi_normalization_does_not_repair_or_casefold(self):
        self.assertEqual(normalize_doi_literal(' 10.1234/AbC '),'10.1234/AbC')
        self.assertEqual(normalize_doi_literal('https://doi.org/10.1234/AbC'),'10.1234/AbC')
        for value in ('https://example.org/10.1234/x','https://doi.org/10.1234/x?query=1',
                      'https://doi.org/10.1234/x#part','invalid','https://[invalid'):
            self.assertIsNone(normalize_doi_literal(value))
        with self.assertRaises(TypeError):normalize_doi_literal(None)

    def test_same_doi_normalization_in_bibitem_and_bibtex(self):
        entry=read_bibtex('@article{bib,doi={http://dx.doi.org/10.1234/a}}',source=SourceContext('p',2)).entries[0]
        a=make_reference_occurrence(prepare_bibtex_entry(entry),occurrence_id='bib')
        b=occurrence('tex',doi='10.1234/a')
        self.assertEqual(len(unify_references([a,b]).merge.records),1)

    def test_audit_is_json_safe_has_scoped_rows_and_all_raw_evidence(self):
        records=[occurrence('a',doi='10.1234/a'),occurrence('b',doi='10.1234/a',version=2)]
        result=unify_references(records)
        payload=json.loads(json.dumps(b3_audit_payload(result),ensure_ascii=False))
        self.assertEqual(len(payload['result']['merge']['mapping']),2)
        self.assertEqual(payload['result']['merge']['occurrences'][0]['prepared']['entry']['raw'],records[0].prepared.entry.raw)
        self.assertEqual(result.merge.mapping.keys(),{('p',1,'a'),('p',2,'b')})

    def test_audit_writer_refuses_overwrite(self):
        result=unify_references([occurrence('a')])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audit.json'
            write_b3_audit(result,path)
            before=path.read_bytes()
            with self.assertRaises(FileExistsError):write_b3_audit(result,path)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(json.loads(before)['result']['policy_id'],result.policy_id)

    def test_cache_hook_returns_reproducible_string_not_bytes(self):
        value=b3_cache_fingerprint()
        self.assertIsInstance(value,str)
        self.assertRegex(value,r'^[0-9a-f]{64}$')
        self.assertEqual(value,b3_cache_fingerprint())

    def test_payload_export_rejects_wrong_result(self):
        with self.assertRaises(TypeError):b3_audit_payload(None)


class FinalizerTests(unittest.TestCase):
    def test_heading_and_leaf_are_both_remapped_after_structural_projection(self):
        ref=occurrence('old',doi='10.1234/a')
        result=unify_references([ref])
        leaf=fixtures.FrozenNode(3,'sentence',r'Body \cite{old}.')
        heading=fixtures.FrozenNode(2,'heading',r'\section{Heading \cite{old}}',children=(leaf,))
        source=fixtures.FrozenHierarchy(root=fixtures.FrozenNode(1,'document','',children=(heading,)))
        normalized=normalize_a3_hierarchy(source).hierarchy
        before=deepcopy(normalized)
        final=finalize_b3_tree(normalized,result,node_factory=SimpleNamespace,
                              structural_content=lambda n:n.raw if n.kind=='heading' else '')
        key=result.merge.records[0].payload.citation_key
        self.assertIn(r'\cite{'+key+'}',final.tree.children[0].content)
        self.assertIn(r'\cite{'+key+'}',final.tree.children[0].children[0].content)
        self.assertEqual(normalized,before)
        self.assertTrue(all(not n.result.warnings for n in final.nodes))
        self.assertEqual(final.nodes[-1].child_path,(0,0))

    def test_already_remapped_leaf_does_not_become_unknown_during_projection(self):
        result=unify_references([occurrence('old',doi='10.1234/a')])
        rich=normalize_a3_hierarchy(fixtures.hierarchy()).hierarchy
        remapped=remap_a3_hierarchy(rich,result.merge.mapping).hierarchy
        final=finalize_b3_tree(remapped,result,node_factory=SimpleNamespace,structural_content=lambda n:'')
        self.assertTrue(all(not node.result.warnings for node in final.nodes))

    def test_unknown_and_ambiguous_cites_are_preserved_in_projected_content(self):
        result=unify_references([occurrence('old',identity='a'),occurrence('old',identity='b')])
        source=normalize_a3_hierarchy(fixtures.hierarchy(r'\cite{old,missing}')).hierarchy
        final=finalize_b3_tree(source,result,node_factory=SimpleNamespace,structural_content=lambda n:'')
        self.assertEqual(final.tree.children[0].content,r'\cite{old,missing}')
        self.assertEqual({d.status for d in final.nodes[-1].result.decisions},{'ambiguous','unresolved'})

    def test_unparseable_tree_stays_unparseable(self):
        source=fixtures.FrozenHierarchy(status='unparseable')
        final=finalize_b3_tree(source,unify_references([]),node_factory=SimpleNamespace,structural_content=lambda n:'')
        self.assertIsNone(final.tree)
        self.assertEqual(final.nodes,())




if __name__=='__main__':
    unittest.main()
