"""B2 stage 1 acceptance tests: raw-preserving bibliography segmentation."""

from dataclasses import FrozenInstanceError, asdict
import json
import random
import unittest

from standardization import extract_bibliography, SourceContext


def bibliography(body, width='99'):
    return '\\begin{thebibliography}{' + width + '}\n' + body + '\\end{thebibliography}'


class BibliographyExtractionTests(unittest.TestCase):
    def check_result(self, raw, **kwargs):
        result = extract_bibliography(raw, **kwargs)
        self.assertEqual(result.raw, raw)
        self.assertEqual(extract_bibliography(raw, **kwargs), result)
        previous_end = 0
        for index, region in enumerate(result.regions):
            self.assertGreaterEqual(region.start, previous_end)
            self.assertEqual(raw[region.start:region.end], region.raw)
            self.assertTrue(region.start <= region.body_start <= region.body_end <= region.end)
            previous_end = region.end
            fragments = [f for f in result.unparsed if f.region_index == index]
            entries = [result.entries[i] for i in region.entry_indexes]
            spans = sorted([(f.start, f.end, f.raw) for f in fragments]
                           + [(e.start, e.end, e.raw) for e in entries])
            cursor = region.body_start
            for start, end, text in spans:
                self.assertEqual(start, cursor)
                self.assertEqual(text, raw[start:end])
                cursor = end
            self.assertEqual(cursor, region.body_end)
        for entry in result.entries:
            self.assertEqual(raw[entry.start:entry.end], entry.raw)
            self.assertEqual(raw[entry.content_start:entry.end], entry.raw_content)
        for fragment in result.unparsed:
            self.assertEqual(raw[fragment.start:fragment.end], fragment.raw)
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_no_bibliography_and_empty_input(self):
        for raw in ('', 'An ordinary paragraph.', r'\section{Introduction}Text.'):
            with self.subTest(raw=raw):
                result = self.check_result(raw)
                self.assertFalse(result.entries)
                self.assertFalse(result.regions)
                self.assertFalse(result.warnings)

    def test_multiple_entries_and_multiline_bodies(self):
        raw = bibliography('\\bibitem{a}A. Author.\nA title.\n\\newblock Journal, 2020.\n'
                           '\\bibitem{b}B. Author. Another title.\n')
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['a', 'b'])
        self.assertEqual(result.entries[0].raw_content,
                         'A. Author.\nA title.\n\\newblock Journal, 2020.\n')
        self.assertTrue(result.regions[0].safe_to_exclude)
        self.assertFalse(result.warnings)

    def test_optional_label_is_distinct_from_key(self):
        raw = bibliography(r'\bibitem[Smith et al.(2020)]{smith2020}Body.' + '\n')
        result = self.check_result(raw)
        entry = result.entries[0]
        self.assertEqual(entry.citation_key, 'smith2020')
        self.assertEqual(entry.optional_label, 'Smith et al.(2020)')

    def test_nested_label_formatting_and_empty_optional_label(self):
        raw = bibliography(r'\bibitem[\protect\citeauthoryear{A [B]}{2020}]{a}One.' + '\n'
                           + r'\bibitem[]{b}Two.' + '\n')
        result = self.check_result(raw)
        self.assertEqual(result.entries[0].optional_label, r'\protect\citeauthoryear{A [B]}{2020}')
        self.assertEqual(result.entries[1].optional_label, '')

    def test_whitespace_and_comments_between_header_arguments(self):
        raw = bibliography('\\bibitem % a comment\n [Label] % another\n {key}\nBody.\n')
        result = self.check_result(raw)
        self.assertEqual(result.entries[0].citation_key, 'key')
        self.assertEqual(result.entries[0].raw_content, '\nBody.\n')

    def test_unicode_source_context_and_input_relative_offsets(self):
        raw = 'Nội dung trước.\n' + bibliography('\\bibitem{k}Nguyễn và cộng sự.\n')
        source = SourceContext('2411.18799', 2, 'parts/references.tex')
        result = self.check_result(raw, source=source)
        entry = result.entries[0]
        self.assertEqual(entry.source, source)
        self.assertEqual(entry.start, raw.index(r'\bibitem'))
        self.assertEqual(raw[entry.content_start:entry.end], 'Nguyễn và cộng sự.\n')

    def test_optional_provenance_and_serialization(self):
        result = self.check_result(bibliography(r'\bibitem{k}Body.'))
        self.assertIsNone(result.entries[0].source)
        payload = json.loads(json.dumps(asdict(result)))
        self.assertEqual(payload['entries'][0]['citation_key'], 'k')

    def test_preamble_is_retained_separately(self):
        raw = bibliography('\\small\nPreface text.\n\\bibitem{k}Body.\n')
        result = self.check_result(raw)
        self.assertEqual(result.unparsed[0].raw, '\n\\small\nPreface text.\n')

    def test_comment_fake_items_and_environments_ignored(self):
        raw = ('% \\begin{thebibliography}{9}\n% \\bibitem{fake}Oops\n'
               + bibliography('% \\bibitem{fake} and \\end{thebibliography}\n'
                              '\\bibitem{real}Body.\n% \\bibitem{fake2}Ignored\n'))
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['real'])
        self.assertEqual(len(result.regions), 1)

    def test_escaped_percent_and_backslash(self):
        raw = bibliography(r'\bibitem{a}10\% and \\bibitem{not-an-entry}.' + '\n'
                           + r'\bibitem{b}Next.' + '\n')
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['a', 'b'])

    def test_verbatim_and_code_environments_hide_fake_markers(self):
        for protected in (r'\verb|\bibitem{fake}|',
                          r'\begin{verbatim}\bibitem{fake}\end{verbatim}',
                          r'\begin{lstlisting}\bibitem{fake}\end{lstlisting}',
                          r'\begin{comment}\bibitem{fake}\end{comment}'):
            with self.subTest(protected=protected):
                result = self.check_result(bibliography(r'\bibitem{a}' + protected + '\n'))
                self.assertEqual([e.citation_key for e in result.entries], ['a'])

    def test_entire_bibliography_inside_code_is_not_extracted(self):
        raw = '\\begin{verbatim}\n' + bibliography(r'\bibitem{fake}Body.') + '\n\\end{verbatim}'
        result = self.check_result(raw)
        self.assertFalse(result.regions)

    def test_math_hides_fake_items(self):
        for protected in (r'$\bibitem{fake}$', r'\(\bibitem{fake}\)',
                          r'\begin{equation}\bibitem{fake}\end{equation}'):
            with self.subTest(protected=protected):
                result = self.check_result(bibliography(r'\bibitem{a}' + protected + '\n'))
                self.assertEqual([e.citation_key for e in result.entries], ['a'])

    def test_macro_arguments_do_not_create_entries(self):
        raw = bibliography(r'\bibitem{a}\textbf{Title \bibitem{fake}} '
                           r'\url{https://a.org/x%20_y} \href{url}{\bibitem{fake2}}' + '\n')
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['a'])
        self.assertIn(r'\bibitem{fake}', result.entries[0].raw_content)

    def test_macro_defined_bibliography_is_deferred(self):
        raw = r'\newcommand{\refs}{' + bibliography(r'\bibitem{fake}Body.') + '}'
        result = self.check_result(raw)
        self.assertFalse(result.regions)
        self.assertIn('macro_bibliography_deferred', [w.code for w in result.warnings])

    def test_macro_definition_control_word_names_do_not_activate_conditionals(self):
        for declaration in (r'\def\ifcustom#1{\iftrue}', r'\newcommand\ifcustom[1]{\iftrue}',
                            r'\renewcommand{\ifcustom}{\iffalse}', r'\newif\ifcustom',
                            r'\let\ifcustom=\iftrue'):
            with self.subTest(declaration=declaration):
                result = self.check_result(declaration + bibliography(r'\bibitem{real}Body.'))
                self.assertEqual([e.citation_key for e in result.entries], ['real'])

    def test_unbraced_macro_definition_bibliography_body_is_not_executed(self):
        for prefix in (r'\def\refs', r'\newcommand\refs'):
            with self.subTest(prefix=prefix):
                raw = prefix + '{' + bibliography(r'\bibitem{fake}Body.') + '}'
                result = self.check_result(raw)
                self.assertFalse(result.regions)
                self.assertIn('macro_bibliography_deferred', [w.code for w in result.warnings])

    def test_malformed_macro_definition_is_reported(self):
        result = self.check_result(r'\newcommand\refs{broken')
        self.assertIn('malformed_macro_definition', [w.code for w in result.warnings])

    def test_grouped_bibliography_is_deferred(self):
        raw = '{\\small ' + bibliography(r'\bibitem{k}Body.') + '}'
        result = self.check_result(raw)
        self.assertFalse(result.exclusion_spans)
        self.assertIn('grouped_bibliography_deferred', [w.code for w in result.warnings])

    def test_conditionals_are_not_evaluated(self):
        raw = r'\iffalse ' + bibliography(r'\bibitem{fake}Body.') + r'\fi '
        raw += bibliography(r'\bibitem{real}Body.')
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['real'])
        self.assertIn('conditional_content_deferred', [w.code for w in result.warnings])

    def test_fake_conditional_end_inside_code_does_not_expose_disabled_bibliography(self):
        raw = (r'\iffalse\begin{verbatim}\fi\end{verbatim}'
               + bibliography(r'\bibitem{fake}Body.') + r'\fi '
               + bibliography(r'\bibitem{real}Body.'))
        result = self.check_result(raw)
        self.assertEqual([e.citation_key for e in result.entries], ['real'])

    def test_nested_conditionals_and_unclosed_conditional(self):
        raw = (r'\iftrue\iffalse ' + bibliography(r'\bibitem{fake}Body.')
               + r'\fi\else Other.\fi ' + bibliography(r'\bibitem{real}Body.'))
        self.assertEqual([e.citation_key for e in self.check_result(raw).entries], ['real'])
        result = self.check_result(r'\iffalse ' + bibliography(r'\bibitem{fake}Body.'))
        self.assertFalse(result.exclusion_spans)
        self.assertTrue(result.warnings)

    def test_begin_end_allow_spacing_and_comments_before_name(self):
        raw = ('\\begin % header\n {thebibliography}{99}\n\\bibitem{k}Body.\n'
               '\\end % footer\n {thebibliography}')
        result = self.check_result(raw)
        self.assertTrue(result.regions[0].safe_to_exclude)
        self.assertEqual(result.entries[0].citation_key, 'k')

    def test_starred_bibitem_is_deferred(self):
        result = self.check_result(bibliography(r'\bibitem*{k}Body.'))
        self.assertFalse(result.exclusion_spans)
        self.assertIn('unsupported_bibitem_form', [w.code for w in result.warnings])

    def test_unclosed_code_or_math_does_not_generate_an_exclusion(self):
        for protected in (r'\begin{verbatim}broken', r'\(broken', r'\begin{equation}broken'):
            with self.subTest(protected=protected):
                result = self.check_result(bibliography(r'\bibitem{k}' + protected))
                self.assertFalse(result.exclusion_spans)

    def test_duplicate_key_across_two_regions_is_retained(self):
        result = self.check_result(bibliography(r'\bibitem{k}One.') + bibliography(r'\bibitem{k}Two.'))
        self.assertEqual(len(result.entries), 2)
        self.assertIn('duplicate_citation_key', [w.code for w in result.entries[1].warnings])

    def test_malformed_mixed_inputs_still_preserve_partition_and_spans(self):
        rng = random.Random(77)
        tokens = [r'\bibitem{k}', r'\bibitem[Broken', r'\bibitem{}', '{', '}', '\n',
                  r'\begin{thebibliography}{9}', r'\end{thebibliography}',
                  r'\custom{body}', r'\(bad', r'\verb|bad', '% comment\n', 'Text.']
        for _ in range(250):
            self.check_result(''.join(rng.choices(tokens, k=rng.randrange(1, 15))))

    def test_duplicate_keys_retained_without_remapping(self):
        result = self.check_result(bibliography(r'\bibitem{k}One.\bibitem{k}Two.'))
        self.assertEqual([e.citation_key for e in result.entries], ['k', 'k'])
        self.assertIn('duplicate_citation_key', [w.code for w in result.entries[1].warnings])
        self.assertTrue(result.regions[0].safe_to_exclude)

    def test_multiple_regions_keep_different_provenance_indexes(self):
        raw = bibliography(r'\bibitem{a}One.') + '\nBody\n' + bibliography(r'\bibitem{b}Two.')
        result = self.check_result(raw)
        self.assertEqual(len(result.regions), 2)
        self.assertEqual([e.region_index for e in result.entries], [0, 1])
        self.assertEqual([r.entry_indexes for r in result.regions], [(0,), (1,)])

    def test_safe_exclusions_keep_appendix_and_acknowledgements(self):
        prefix = r'\section*{Acknowledgements}Thanks.'
        appendix = r'\appendix\section{Proof}Must remain.'
        raw = prefix + bibliography(r'\bibitem{k}Body.') + appendix
        result = self.check_result(raw)
        body = raw
        for start, end in reversed(result.exclusion_spans):
            body = body[:start] + body[end:]
        self.assertEqual(body, prefix + appendix)

    def test_references_heading_outside_environment_not_silently_removed(self):
        raw = r'\section*{References}' + bibliography(r'\bibitem{k}Body.')
        result = self.check_result(raw)
        self.assertEqual(result.regions[0].start, len(r'\section*{References}'))

    def test_missing_end_never_excludes_document_tail(self):
        raw = '\\begin{thebibliography}{9}\n\\bibitem{k}Body.\n\\appendix Proof.'
        result = self.check_result(raw)
        self.assertFalse(result.regions[0].closed)
        self.assertFalse(result.exclusion_spans)
        self.assertIn('unclosed_bibliography', [w.code for w in result.warnings])

    def test_missing_width_argument_retains_entries_but_is_not_safe(self):
        raw = r'\begin{thebibliography}\bibitem{k}Body.\end{thebibliography}'
        result = self.check_result(raw)
        self.assertEqual(result.entries[0].citation_key, 'k')
        self.assertFalse(result.regions[0].header_valid)
        self.assertFalse(result.exclusion_spans)

    def test_empty_and_whitespace_key_are_not_guessed(self):
        for key in ('', ' foo ', r'\macro'):
            with self.subTest(key=key):
                result = self.check_result(bibliography('\\bibitem{' + key + '}Body.'))
                self.assertIsNone(result.entries[0].citation_key)
                self.assertEqual(result.entries[0].citation_key_raw, key)
                self.assertFalse(result.exclusion_spans)
                self.assertIn('invalid_citation_key', [w.code for w in result.warnings])

    def test_missing_key_does_not_suppress_next_valid_entry(self):
        result = self.check_result(bibliography(r'\bibitem Missing.\bibitem{good}Valid.'))
        self.assertEqual([e.citation_key for e in result.entries], [None, 'good'])
        self.assertFalse(result.exclusion_spans)

    def test_unclosed_optional_label_retains_all_remaining_raw(self):
        raw = bibliography(r'\bibitem[Broken{key}Text.\bibitem{later}Later.') + r'\appendix Proof.'
        result = self.check_result(raw)
        self.assertFalse(result.exclusion_spans)
        self.assertEqual(len(result.entries), 1)
        self.assertIn('unclosed_bibitem_label', [w.code for w in result.warnings])

    def test_unclosed_key_and_unknown_macro_arguments(self):
        for raw in (r'\begin{thebibliography}{9}\bibitem{broken',
                    r'\begin{thebibliography}{9}\bibitem{k}\custom{broken'):
            with self.subTest(raw=raw):
                result = self.check_result(raw)
                self.assertFalse(result.exclusion_spans)
                self.assertTrue(result.warnings)

    def test_nested_bibliography_retained_as_unparsed_body(self):
        raw = bibliography(r'\bibitem{outer}A.' + bibliography(r'\bibitem{inner}B.'))
        result = self.check_result(raw)
        self.assertFalse(result.entries)
        self.assertFalse(result.exclusion_spans)
        self.assertIn('nested_bibliography', [w.code for w in result.warnings])

    def test_nested_environment_bibitem_not_silently_treated_as_empty_complete(self):
        raw = bibliography(r'\begin{multicols}{2}\bibitem{k}Body.\end{multicols}')
        result = self.check_result(raw)
        self.assertFalse(result.exclusion_spans)
        self.assertIn('nested_bibitem_deferred', [w.code for w in result.warnings])

    def test_mismatched_environment_not_safe(self):
        raw = bibliography(r'\bibitem{k}\begin{quote}Body.')
        result = self.check_result(raw)
        self.assertTrue(result.regions[0].closed)
        self.assertFalse(result.exclusion_spans)
        self.assertIn('mismatched_environment', [w.code for w in result.warnings])

    def test_empty_bibliography_retains_body(self):
        result = self.check_result(bibliography(' \n% no entries\n'))
        self.assertFalse(result.entries)
        self.assertTrue(result.regions[0].safe_to_exclude)
        self.assertIn('empty_bibliography', [w.code for w in result.warnings])

    def test_empty_entry_body_warns(self):
        result = self.check_result(bibliography(r'\bibitem{k}' + '\n'))
        self.assertIn('empty_bibitem_content', [w.code for w in result.warnings])

    def test_external_bib_files_deferred_not_read(self):
        for raw in (r'\bibliography{refs}', r'\addbibresource{refs.bib}', r'\printbibliography'):
            with self.subTest(raw=raw):
                result = self.check_result(raw)
                self.assertFalse(result.entries)
                self.assertIn('external_bibliography_deferred', [w.code for w in result.warnings])

    def test_orphan_item_and_end_are_diagnostic_only(self):
        result = self.check_result(r'\bibitem{k}Body.\end{thebibliography}')
        self.assertFalse(result.regions)
        self.assertFalse(result.entries)
        self.assertEqual([w.code for w in result.warnings], ['orphan_bibitem', 'orphan_bibliography_end'])

    def test_no_metadata_inferred_or_bodies_normalized(self):
        raw = bibliography(r'\bibitem{k}A. Author. \emph{A title}. 2020. \url{https://a.org/doi}  ')
        entry = self.check_result(raw).entries[0]
        self.assertEqual(entry.raw_content, r'A. Author. \emph{A title}. 2020. \url{https://a.org/doi}  ')
        self.assertNotIn('year', asdict(entry))
        self.assertNotIn('entry_type', asdict(entry))

    def test_results_and_source_are_immutable(self):
        result = self.check_result(bibliography(r'\bibitem{k}Body.'))
        with self.assertRaises(FrozenInstanceError):
            result.entries[0].citation_key = 'other'

    def test_input_type_and_source_validation(self):
        with self.assertRaises(TypeError):
            extract_bibliography(None)
        with self.assertRaises(TypeError):
            extract_bibliography('', source={})
        with self.assertRaises(ValueError):
            SourceContext(version=True)

    def test_deterministic_mixed_fixture_span_invariants(self):
        rng = random.Random(2026)
        bodies = ['Plain title.\n', r'\textbf{Title \bibitem{fake}}', r'\verb|\bibitem{x}|',
                  r'$\bibitem{x}$', '\n% \\bibitem{fake}\n', 'Nguyễn. 2020.\n']
        for _ in range(250):
            body = ''.join('\\bibitem{k' + str(i) + '}' + rng.choice(bodies)
                           for i in range(rng.randrange(1, 8)))
            self.check_result('Prefix\n' + bibliography(body) + '\nAppendix')


if __name__ == '__main__':
    unittest.main()
