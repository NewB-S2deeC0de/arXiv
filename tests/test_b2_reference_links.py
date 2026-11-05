"""DOI/URL evidence tests; does not assert registration or final metadata."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import random
import unittest

from standardization import extract_bibliography, extract_reference_links


class ReferenceLinksTests(unittest.TestCase):
    def check(self, raw):
        result = extract_reference_links(raw)
        self.assertEqual(result.raw, raw)
        self.assertEqual(result, extract_reference_links(raw))
        self.assertEqual(list(result.candidates),
                         sorted(result.candidates, key=lambda x: (x.start, x.end, x.kind)))
        for item in result.candidates:
            self.assertTrue(0 <= item.start < item.end <= len(raw))
            self.assertEqual(item.raw, raw[item.start:item.end])
            self.assertEqual(item.value, item.raw)
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def values(self, raw, kind):
        return [x.value for x in self.check(raw).candidates if x.kind == kind]

    def test_empty_and_ordinary_text(self):
        for raw in ('', 'Smith, 2024. A paper.'):
            self.assertFalse(self.check(raw).candidates)

    def test_reject_nonstring(self):
        for raw in (None, 3, b'url'):
            with self.assertRaises(TypeError):
                extract_reference_links(raw)

    def test_plain_doi_preserves_case_and_punctuation_inside(self):
        self.assertEqual(self.values('doi:10.1234/Ab_C-9(2):X', 'doi'), ['10.1234/Ab_C-9(2):X'])

    def test_invalid_doi_prefix_and_empty_suffix_are_not_candidates(self):
        for raw in ('10.123/a', '10.1234567890/a', '10.1234/', '10.1234/.', 'x10.1234/abc'):
            self.assertFalse(self.check(raw).candidates, raw)

    def test_url_command_yields_url_and_nested_doi_evidence(self):
        r = self.check(r'\url{https://doi.org/10.1021/ac048539t}')
        self.assertEqual([x.kind for x in r.candidates], ['url', 'doi'])
        self.assertEqual(r.candidates[1].value, '10.1021/ac048539t')
        self.assertEqual(r.candidates[0].source_form, 'url')
        self.assertEqual(r.candidates[1].source_form, 'doi_url')

    def test_href_target_scanned_display_is_not(self):
        r = self.check(r'\href{https://example.org/paper}{10.9999/display-only}')
        self.assertEqual([x.value for x in r.candidates], ['https://example.org/paper'])

    def test_href_with_percent_query_and_fragment(self):
        self.assertEqual(self.values(r'\href{https://example.org/a%20b?q=1&x=2#top}{Read}', 'url'),
                         ['https://example.org/a%20b?q=1&x=2#top'])

    def test_doi_resolver_query_is_only_in_url(self):
        raw = r'\url{HTTPS://DX.DOI.ORG/10.1234/ABC?q=1#fragment}'
        self.assertEqual(self.values(raw, 'doi'), ['10.1234/ABC'])
        self.assertEqual(self.values(raw, 'url'), ['HTTPS://DX.DOI.ORG/10.1234/ABC?q=1#fragment'])

    def test_unrecognized_resolver_retains_url_with_warning(self):
        r = self.check(r'\url{https://doi.org/not-a-doi}')
        self.assertEqual([x.kind for x in r.candidates], ['url'])
        self.assertIn('unrecognized_doi_url', [w.code for w in r.warnings])

    def test_bare_url_punctuation_is_trimmed_with_warning(self):
        r = self.check('(https://example.org/a).')
        self.assertEqual(r.candidates[0].value, 'https://example.org/a')
        self.assertIn('ambiguous_trailing_punctuation', [w.code for w in r.warnings])

    def test_balanced_parentheses_are_kept(self):
        self.assertEqual(self.values('https://example.org/a(b)', 'url'), ['https://example.org/a(b)'])
        self.assertEqual(self.values('10.1234/a(b).', 'doi'), ['10.1234/a(b)'])

    def test_braced_url_does_not_trim_target_punctuation(self):
        self.assertEqual(self.values(r'\url{https://example.org/a.}', 'url'), ['https://example.org/a.'])

    def test_all_occurrences_retained_no_selection_or_dedup(self):
        raw = '10.1234/a 10.1234/a 10.9999/b'
        self.assertEqual(self.values(raw, 'doi'), ['10.1234/a', '10.1234/a', '10.9999/b'])

    def test_unicode_offsets_are_input_character_offsets(self):
        raw = 'Nguyễn: 10.1234/ABC'
        r = self.check(raw)
        self.assertEqual(r.candidates[0].start, len('Nguyễn: '))

    def test_comments_and_code_are_skipped(self):
        raw = ('% 10.1234/comment\n' + r'\verb|https://example.org/code|' +
               r'\begin{verbatim}10.1234/code\end{verbatim}' +
               r'\begin{comment}10.1234/hidden\end{comment}' + ' 10.1234/real')
        self.assertEqual(self.values(raw, 'doi'), ['10.1234/real'])

    def test_math_is_skipped(self):
        raw = r'$10.1234/math$ \(10.1234/math\) \begin{equation}10.1234/math\end{equation}'
        self.assertFalse(self.check(raw).candidates)

    def test_formatting_and_group_content_is_scanned(self):
        raw = r'\emph{See {\textbf{10.1234/a}}} {https://example.org/b}'
        self.assertEqual([x.value for x in self.check(raw).candidates],
                         ['10.1234/a', 'https://example.org/b'])

    def test_unknown_command_arguments_and_citations_are_deferred(self):
        r = self.check(r'\custom{10.1234/no}\cite{10.1234/key} 10.1234/yes')
        self.assertEqual([x.value for x in r.candidates], ['10.1234/yes'])
        self.assertIn('link_command_arguments_deferred', [w.code for w in r.warnings])

    def test_optional_formatting_form_is_deferred_with_warning(self):
        r = self.check(r'\emph[option]{10.1234/no} 10.1234/yes')
        self.assertEqual([x.value for x in r.candidates], ['10.1234/yes'])
        self.assertIn('link_formatting_deferred', [w.code for w in r.warnings])

    def test_bare_url_percent_encoding_and_percent_outside_target(self):
        raw = 'https://example.org/a%20b\n% 10.1234/comment\n10.1234/yes'
        self.assertEqual([x.value for x in self.check(raw).candidates],
                         ['https://example.org/a%20b', '10.1234/yes'])

    def test_generic_environment_is_deferred_with_warning(self):
        r = self.check(r'\begin{custom}10.1234/no\end{custom} 10.1234/yes')
        self.assertEqual([x.value for x in r.candidates], ['10.1234/yes'])
        self.assertIn('link_environment_deferred', [w.code for w in r.warnings])

    def test_macro_definitions_are_not_executed(self):
        self.assertEqual(self.values(r'\newcommand{\x}{10.1234/no}\def\y{10.1234/no} 10.1234/yes', 'doi'),
                         ['10.1234/yes'])

    def test_conditional_content_is_deferred(self):
        r = self.check(r'\iffalse 10.1234/no\else 10.1234/no\fi 10.1234/yes')
        self.assertEqual([x.value for x in r.candidates], ['10.1234/yes'])
        self.assertIn('link_conditional_deferred', [w.code for w in r.warnings])

    def test_newif_and_let_targets_do_not_start_conditionals(self):
        self.assertEqual(self.values(r'\newif\ifx \let\ifalias=\ifx 10.1234/yes', 'doi'), ['10.1234/yes'])

    def test_nonliteral_or_unsupported_targets_are_not_guessed(self):
        for raw in (r'\url{https://example.org/a\_b}', r'\url{https://example.org/\macro}',
                    r'\url{mailto:author@example.org}', r'\url{https://example.org/a b}', r'\url{https://}'):
            r = self.check(raw)
            self.assertFalse(r.candidates)
            self.assertTrue(r.warnings)

    def test_malformed_commands_and_constructs_keep_tail_and_warn(self):
        for tail in (r'\url{https://example.org/a', r'\href{https://example.org/a}',
                     r'\url https://example.org/a', '$10.1234/bad', '{10.1234/bad',
                     r'\verb|10.1234/bad', r'\iffalse 10.1234/bad'):
            raw = '10.1234/good ' + tail
            r = self.check(raw)
            self.assertEqual([x.value for x in r.candidates], ['10.1234/good'])
            self.assertTrue(r.warnings)

    def test_depth_limit_is_reported(self):
        r = self.check('{' * 70 + '10.1234/deep' + '}' * 70)
        self.assertFalse(r.candidates)
        self.assertIn('link_scan_depth_limit', [w.code for w in r.warnings])

    def test_result_records_are_immutable(self):
        r = self.check('10.1234/a')
        with self.assertRaises(FrozenInstanceError):
            r.candidates[0].value = 'changed'


    def test_seeded_mixed_inputs_preserve_spans_and_determinism(self):
        rng = random.Random(20261103)
        chunks = ['10.1234/a ', r'\url{https://doi.org/10.9999/b}',
                  r'\emph{https://example.org/a}', '% 10.1234/no\n',
                  r'\verb|10.1234/no|', r'\custom{10.1234/no}', 'Nguyễn ',
                  r'$10.1234/no$', r'\href{https://example.org/a%20b}{link}']
        for _ in range(250):
            self.check(' '.join(rng.choices(chunks, k=8)))


if __name__ == '__main__':
    unittest.main()
