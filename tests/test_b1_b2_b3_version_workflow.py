"""Regression tests for frozen C nodes, literal BibTeX and explicit B3 decisions."""
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace as Obj
import unittest

from standardization import (normalize_a3_hierarchy, read_bibtex, SourceContext,
    to_c_tree, remap_a3_hierarchy, extract_bibliography, prepare_c_bibentry,
    make_reference_occurrence, merge_reference_decisions, remap_citations, prepare_bibtex_entry,
    analyze_reference_candidates,RawSourceMap,SourceMapPiece,MappedSourceSpan)


@dataclass(frozen=True)
class FrozenNode:
    occurrence_id: int
    kind: str
    raw: str
    source_spans: tuple = ()
    children: tuple = ()
    attrs: dict = field(default_factory=dict)
    content: str | None = None
    warnings: tuple = ()


@dataclass(frozen=True)
class FrozenHierarchy:
    arxiv_id: str = 'p'
    version: int = 1
    status: str = 'ready'
    root: FrozenNode | None = None
    bibliography: tuple = ()
    diagnostics: tuple = ()


def hierarchy(raw=r'Use \textbf{this} \cite{old}.', *, kind='sentence', version=1):
    leaf = FrozenNode(2, kind, raw, attrs={'untouched': ['value']})
    return FrozenHierarchy(version=version, root=FrozenNode(1, 'document', 'Body', children=(leaf,)))


def occurrence(key, *, version=1, identity=None, doi=None, metadata=None, pub='p'):
    raw = r'\begin{thebibliography}{1}\bibitem{' + key + '}Reference.'
    if doi:
        raw += r'\url{https://doi.org/' + doi + '}'
    raw += r'\end{thebibliography}'
    entry = extract_bibliography(raw, source=SourceContext(pub, version)).entries[0]
    return make_reference_occurrence(prepare_c_bibentry(entry, metadata=metadata),
                                     occurrence_id=identity or key)


class FrozenWorkflowTests(unittest.TestCase):
    def test_frozen_tree_normalizes_without_mutating_raw_attrs_or_input(self):
        original = hierarchy()
        before = deepcopy(original)
        result = normalize_a3_hierarchy(original)
        self.assertEqual(original, before)
        self.assertIsNone(original.root.children[0].content)
        leaf = result.hierarchy.root.children[0]
        self.assertEqual(leaf.content, r'Use this \cite{old}.')
        self.assertEqual(leaf.raw, original.root.children[0].raw)
        self.assertIsInstance(result.hierarchy.root.children, tuple)
        leaf.attrs['untouched'].append('copy')
        self.assertEqual(original, before)

    def test_partial_frozen_unsplit_paragraph_is_preserved(self):
        original = replace(hierarchy(kind='paragraph'), status='partial')
        result = normalize_a3_hierarchy(original)
        self.assertEqual(result.hierarchy.status, 'partial')
        self.assertEqual(result.hierarchy.root.children[0].content, original.root.children[0].raw)
        self.assertEqual(result.diagnostics[0].code, 'a3_paragraph_unresolved')

    def test_projection_requires_finalized_leaf_and_explicit_container_policy(self):
        with self.assertRaises(ValueError):
            to_c_tree(hierarchy(), node_factory=Obj, structural_content=lambda n: '')
        normalized = normalize_a3_hierarchy(hierarchy()).hierarchy
        tree = to_c_tree(normalized, node_factory=Obj, structural_content=lambda n: '')
        self.assertEqual(tree.content, '')
        self.assertEqual(tree.children[0].content, r'Use this \cite{old}.')
        self.assertFalse(hasattr(tree, 'raw'))  # Explicitly lossy C2 projection.

    def test_projection_policy_cannot_mutate_rich_tree(self):
        normalized = normalize_a3_hierarchy(hierarchy()).hierarchy
        before = deepcopy(normalized)
        def policy(node):
            node.attrs['changed'] = True
            return node.raw
        to_c_tree(normalized, node_factory=Obj, structural_content=policy)
        self.assertEqual(normalized, before)

    def test_scoped_remap_on_content_preserves_raw_and_frozen_source(self):
        normalized = normalize_a3_hierarchy(hierarchy()).hierarchy
        before = deepcopy(normalized)
        result = remap_a3_hierarchy(normalized, {('p', 1, 'old'): 'canonical', ('p', 2, 'old'): 'other'})
        leaf = result.hierarchy.root.children[0]
        self.assertIn(r'\cite{canonical}', leaf.content)
        self.assertEqual(leaf.raw, before.root.children[0].raw)
        self.assertEqual(normalized, before)
        self.assertEqual(result.nodes[0].offset_basis, 'node_content_before_remap')
        edit = result.nodes[0].result.edits[0]
        self.assertEqual(normalized.root.children[0].content[edit.start:edit.end], 'old')

    def test_remap_unknowns_are_audited_and_unfinalized_is_rejected(self):
        with self.assertRaises(ValueError):
            remap_a3_hierarchy(hierarchy(), {})
        result = remap_a3_hierarchy(normalize_a3_hierarchy(hierarchy()).hierarchy, {})
        self.assertEqual(result.nodes[0].result.decisions[0].status, 'unresolved')

    def test_unparseable_projection_and_remap_have_no_usable_tree(self):
        empty = FrozenHierarchy(status='unparseable')
        self.assertIsNone(to_c_tree(empty, node_factory=Obj, structural_content=lambda n: ''))
        self.assertIsNone(remap_a3_hierarchy(empty, {}).hierarchy.root)

    def test_remap_keeps_figure_math_code_and_non_key_text(self):
        raw = r'\begin{figure}\caption{old \cite{old}}\verb|\cite{old}|\end{figure}'
        normalized = normalize_a3_hierarchy(hierarchy(raw, kind='figure')).hierarchy
        result = remap_a3_hierarchy(normalized, {('p', 1, 'old'): 'new'})
        self.assertEqual(result.hierarchy.root.children[0].content,
                         r'\begin{figure}\caption{old \cite{new}}\verb|\cite{old}|\end{figure}')


class BibtexReaderTests(unittest.TestCase):
    def read(self, raw):
        result = read_bibtex(raw, source=SourceContext('p', 2, 'refs.bib'))
        pieces = sorted([(e.start, e.end, e.raw) for e in result.entries] +
                        [(e.start, e.end, e.raw) for e in result.unparsed])
        cursor = 0
        for start, end, text in pieces:
            self.assertEqual(start, cursor)
            self.assertEqual(raw[start:end], text)
            cursor = end
        self.assertEqual(cursor, len(raw))
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_nested_braces_quotes_commas_and_parentheses(self):
        raw = '@Article(key, title="A {nested, {T}} title", year=2024, note={a (b), c})'
        entry = self.read(raw).entries[0]
        self.assertEqual(entry.entry_type, 'article')
        self.assertEqual([f.value for f in entry.fields], ['A {nested, {T}} title', '2024', 'a (b), c'])

    def test_literal_concatenation_and_symbolic_macro_are_distinct(self):
        result = self.read('@misc{k,title={A} # "B" # 12,month=jan}')
        self.assertEqual(result.entries[0].fields[0].value, 'AB12')
        self.assertIsNone(result.entries[0].fields[1].value)
        self.assertIn('bibtex_value_deferred', [w.code for w in result.warnings])

    def test_directives_comments_and_fake_entries_not_executed(self):
        raw = '% @misc{fake,title={X}}\n@string{x="Value"}\n@comment{ @misc{fake2} }\n@preamble{"X"}\n@misc{real,title=x}'
        result = self.read(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['real'])
        self.assertIsNone(result.entries[0].fields[0].value)
        self.assertEqual(sum(w.code == 'bibtex_directive_deferred' for w in result.warnings), 2)

    def test_duplicate_keys_and_fields_are_retained(self):
        result = self.read('@misc{k,title={A},title={B}}@book{k,year=2020}')
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(len(result.entries[0].fields), 2)
        self.assertIn('bibtex_duplicate_key', [w.code for w in result.warnings])
        self.assertIn('bibtex_duplicate_field', [w.code for w in result.warnings])

    def test_inheritance_is_explicitly_deferred(self):
        result = self.read('@inproceedings{k,crossref={parent},xdata={shared}}')
        self.assertEqual(sum(w.code == 'bibtex_inheritance_deferred' for w in result.warnings), 2)

    def test_unclosed_record_keeps_remainder_and_does_not_guess_recovery(self):
        result = self.read('@misc{ok,title={Good}}\n@misc{bad,title={Unclosed\n@misc{fake,title={X}}')
        self.assertEqual([e.citation_key for e in result.entries], ['ok'])
        self.assertIn('bibtex_unclosed_record', [w.code for w in result.warnings])

    def test_top_level_comment_closing_brace_does_not_end_entry(self):
        result = self.read('@misc{k,\n% } @fake{anything}\ntitle={A % literal},year=2024}')
        self.assertEqual([f.value for f in result.entries[0].fields], ['A % literal', '2024'])

    def test_escaped_quotes_and_balanced_tex_are_preserved(self):
        result = self.read(r'@misc{k,title="A \"quote and {\LaTeX}",url={https://x/a?b=c&d=e}}')
        self.assertIn(r'\"quote', result.entries[0].fields[0].value)

    def test_invalid_key_field_and_empty_value_remain_audited(self):
        result = self.read('@misc{bad key,badfield,title=,year=2024}')
        self.assertIsNone(result.entries[0].citation_key)
        self.assertTrue({'bibtex_invalid_key','bibtex_malformed_field','bibtex_value_deferred'} <=
                        {w.code for w in result.warnings})


    def test_empty_and_non_bib_text_are_lossless(self):
        for raw in ('', 'Plain header\n', '@broken text'):
            self.assertEqual(self.read(raw).entries, ())


class BibtexProjectionTests(unittest.TestCase):
    def project(self, raw):
        entry = read_bibtex(raw,source=SourceContext('p',1,'refs.bib')).entries[0]
        before = deepcopy(entry)
        result = prepare_bibtex_entry(entry)
        self.assertEqual(entry,before)
        return result

    def test_literal_fields_and_ordered_authors(self):
        result = self.project('@article{k,title={A {TeX} Title},author={Last, First and A. Other},'
                              'year={2024},doi={10.1234/test},url={https://example.org/x},journal={J}}')
        self.assertEqual(result.payload.authors,('Last, First','A. Other'))
        self.assertEqual(result.payload.year,2024)
        self.assertEqual(result.payload.title,'A {TeX} Title')
        self.assertEqual(result.unknown_fields,())

    def test_corporate_and_is_not_author_separator(self):
        result = self.project('@misc{k,author={{Research and Development} and Doe, Jane}}')
        self.assertEqual(result.payload.authors,('{Research and Development}','Doe, Jane'))

    def test_incomplete_authors_remain_unknown(self):
        result = self.project('@misc{k,author={A. Author and others}}')
        self.assertEqual(result.payload.authors,())
        self.assertIn('authors',result.unknown_fields)
        self.assertIn('bibtex_authors_deferred',[w.code for w in result.warnings])

    def test_duplicates_are_not_selected_even_when_equal(self):
        result = self.project('@misc{k,title={A},title={A}}')
        self.assertIsNone(result.payload.title)
        self.assertEqual(len(result.entry.fields),2)

    def test_macros_inheritance_and_invalid_fields_not_guessed(self):
        result = self.project('@misc{k,title=macro,year={2024-2025},doi={not a DOI},url={file:///x},crossref={parent}}')
        self.assertTrue({'title','year','doi','url'} <= set(result.unknown_fields))
        self.assertEqual(result.payload.entry_type,'misc')
        self.assertIn('bibtex_inheritance_deferred',[w.code for w in result.warnings])

    def test_invalid_original_key_has_no_exportable_payload(self):
        self.assertIsNone(self.project('@misc{bad key,title={A}}').payload)

    def test_resolver_doi_urls_have_exact_identifier_without_network(self):
        for url in ('https://doi.org/10.1234/X','http://dx.doi.org/10.1234/X'):
            result=self.project('@misc{k,doi={'+url+'}}')
            self.assertEqual(result.payload.doi,'10.1234/X')
            self.assertEqual(result.entry.fields[0].value,url)
        for url in ('https://example.org/10.1234/X','https://doi.org/10.1234/X?query=1'):
            self.assertIsNone(self.project('@misc{k,doi={'+url+'}}').payload.doi)

    def test_publication_identity_must_be_explicit_and_match_source(self):
        entry = read_bibtex('@misc{k,title={A}}').entries[0]
        with self.assertRaises(ValueError):
            prepare_bibtex_entry(entry)
        self.assertEqual(prepare_bibtex_entry(entry,arxiv_id='p').payload.arxiv_id,'p')
        entry = replace(entry,source=SourceContext('p',1))
        with self.assertRaises(ValueError):
            prepare_bibtex_entry(entry,arxiv_id='q')



class ExplicitMergeTests(unittest.TestCase):
    def test_complementary_fields_merge_and_map_versioned_keys(self):
        a = occurrence('old', doi='10.1234/x', metadata={'title':'Title','year':2020})
        b = occurrence('new', version=2, doi='10.1234/x', metadata={'authors':['A. Author'],'journal':'J'})
        original = deepcopy([a,b])
        assignments = {a.ref:'canonical', b.ref:'canonical'}
        merged = merge_reference_decisions([a,b], assignments)
        self.assertEqual([a,b], original)
        self.assertEqual(len(merged.records), 1)
        self.assertEqual(merged.records[0].payload.title, 'Title')
        self.assertEqual(merged.records[0].payload.authors, ('A. Author',))
        self.assertEqual(merged.mapping, {('p',1,'old'):'canonical',('p',2,'new'):'canonical'})
        self.assertEqual(merged, merge_reference_decisions([b,a], assignments))

    def test_metadata_conflict_does_not_choose_one_value(self):
        a = occurrence('a', doi='10.1234/x', metadata={'year':2020})
        b = occurrence('b', version=2, doi='10.1234/x', metadata={'year':2021})
        record = merge_reference_decisions([a,b], {a.ref:'final',b.ref:'final'}).records[0]
        self.assertIsNone(record.payload.year)
        self.assertIn('year', record.unknown_fields)
        self.assertEqual([v.value for v in record.conflicts[0].values], [2020,2021])

    def test_different_doi_cannot_merge_despite_assignments(self):
        a,b = occurrence('a',doi='10.1234/a'),occurrence('b',version=2,doi='10.1234/b')
        with self.assertRaises(ValueError):
            merge_reference_decisions([a,b], {a.ref:'final',b.ref:'final'})

    def test_no_doi_does_not_auto_merge_and_unassigned_stays_unresolved(self):
        a,b = occurrence('a'),occurrence('b',version=2)
        result = merge_reference_decisions([a,b], {})
        self.assertFalse(result.records)
        self.assertEqual(result.unresolved, (a.ref,b.ref))
        self.assertEqual(result.mapping, {})

    def test_same_old_key_different_version_has_independent_targets(self):
        a,b = occurrence('same',doi='10.1234/a'),occurrence('same',version=2,doi='10.1234/b')
        result = merge_reference_decisions([a,b], {a.ref:'ref_a',b.ref:'ref_b'})
        self.assertEqual(remap_citations(r'\cite{same}', result.mapping, arxiv_id='p', version=2).content,
                         r'\cite{ref_b}')

    def test_same_version_duplicate_key_ambiguity_maps_to_none(self):
        a,b = occurrence('same',identity='one'),occurrence('same',identity='two')
        result = merge_reference_decisions([a,b], {a.ref:'first',b.ref:'second'})
        self.assertIsNone(result.mapping['p',1,'same'])
        self.assertEqual(len(result.mapping_conflicts), 1)

    def test_partial_assignment_of_duplicate_key_stays_ambiguous(self):
        a,b = occurrence('same',identity='one'),occurrence('same',identity='two')
        result = merge_reference_decisions([a,b], {a.ref:'first'})
        self.assertIsNone(result.mapping['p',1,'same'])
        self.assertEqual(result.unresolved, (b.ref,))

    def test_same_key_in_other_publication_is_isolated(self):
        a,b = occurrence('same'),occurrence('same',pub='q')
        result = merge_reference_decisions([a,b], {a.ref:'final',b.ref:'final'})
        self.assertEqual(len(result.records), 2)

    def test_canonical_collision_with_unresolved_old_key_is_rejected(self):
        a,b = occurrence('a'),occurrence('final',version=2)
        with self.assertRaises(ValueError):
            merge_reference_decisions([a,b], {a.ref:'final'})

    def test_canonical_chain_is_rejected(self):
        a,b = occurrence('a'),occurrence('b')
        with self.assertRaises(ValueError):
            merge_reference_decisions([a,b], {a.ref:'b',b.ref:'c'})

    def test_unknown_defaults_do_not_overwrite_known_metadata(self):
        a = occurrence('a',metadata={'entry_type':'article','authors':['Known']})
        b = occurrence('b',version=2)
        record = merge_reference_decisions([a,b], {a.ref:'final',b.ref:'final'}).records[0]
        self.assertEqual(record.payload.entry_type, 'article')
        self.assertEqual(record.payload.authors, ('Known',))

    def test_invalid_decision_identity_or_key_is_rejected(self):
        a,b = occurrence('a'),occurrence('b')
        for assignments in ({b.ref:'final'}, {a.ref:'bad key'}, {a.ref:'*'}):
            with self.assertRaises(ValueError):
                merge_reference_decisions([a], assignments)


class BibtexB3Tests(unittest.TestCase):
    def occurrence(self,raw,version=1,source_map=None):
        entry = read_bibtex(raw,source=SourceContext('p',version,'refs.bib')).entries[0]
        return make_reference_occurrence(prepare_bibtex_entry(entry),occurrence_id='bib:0',source_map=source_map)

    def test_bibtex_and_bibitem_can_merge_by_explicit_decisions(self):
        a = self.occurrence('@article{a,title={Title},doi={10.1234/x},year=2020}')
        b = occurrence('b',version=2,doi='10.1234/x',metadata={'authors':['A. Author']})
        self.assertEqual(len(analyze_reference_candidates([a,b]).groups),1)
        result = merge_reference_decisions([a,b],{a.ref:'final',b.ref:'final'})
        self.assertEqual(result.records[0].payload.authors,('A. Author',))
        self.assertEqual(result.records[0].payload.title,'Title')

    def test_exact_bib_provenance_does_not_use_bibitem_content_offsets(self):
        raw='@article{a,title={Title},doi={10.1234/x}}'
        mapping=RawSourceMap(raw,(SourceMapPiece(0,len(raw),MappedSourceSpan('refs.bib',0,len(raw))),))
        item=self.occurrence(raw,source_map=mapping)
        self.assertEqual(item.source_mapping,'exact_segments')
        self.assertEqual(item.source_spans,(MappedSourceSpan('refs.bib',0,len(raw)),))

    def test_duplicate_doi_field_cannot_bridge_groups(self):
        a=self.occurrence('@misc{a,doi={10.1234/x},doi={10.1234/x}}')
        b=occurrence('b',version=2,doi='10.1234/x')
        review=analyze_reference_candidates([a,b])
        self.assertFalse(review.groups)
        with self.assertRaises(ValueError):
            merge_reference_decisions([a,b],{a.ref:'final',b.ref:'final'})

    def test_symbolic_doi_remains_unselected(self):
        a=self.occurrence('@misc{a,doi=identifier}')
        result=analyze_reference_candidates([a])
        self.assertFalse(result.records[0].eligible)
        self.assertIsNone(result.records[0].selected_doi)

    def test_resolver_url_and_plain_doi_are_same_unambiguous_candidate(self):
        a=self.occurrence('@misc{a,doi={https://doi.org/10.1234/x}}')
        b=occurrence('b',version=2,doi='10.1234/x')
        result=analyze_reference_candidates([a,b])
        self.assertTrue(all(r.eligible for r in result.records))
        self.assertEqual(len(result.groups),1)


if __name__ == '__main__':
    unittest.main()
