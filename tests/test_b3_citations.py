"""B3 literal remap tests: scope, edit boundaries and real LaTeX sources."""
from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import random
import re
import unittest

from standardization import scan_citations, remap_citations


def mapping(**keys):
    return {('p', 1, old): new for old, new in keys.items()}


class CitationTests(unittest.TestCase):
    def scan(self, raw):
        result = scan_citations(raw)
        self.assertEqual(result.raw, raw)
        self.assertEqual(result, scan_citations(raw))
        previous = 0
        for citation in result.citations:
            self.assertTrue(previous <= citation.start < citation.end <= len(raw))
            self.assertEqual(citation.raw, raw[citation.start:citation.end])
            for argument in citation.optional_arguments:
                self.assertEqual(argument.raw, raw[argument.start:argument.end])
            for key in citation.keys:
                self.assertEqual(key.raw, raw[key.start:key.end])
            previous = citation.end
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def remap(self, raw, given=None, *, pub='p', version=1):
        result = remap_citations(raw, mapping(foo='ref_a', bar='ref_b') if given is None else given,
                                arxiv_id=pub, version=version)
        self.assertEqual(result.raw, raw)
        # Oracle: every edit must be exactly one key of a valid recognized cite.
        allowed = {(k.start, k.end) for c in self.scan(raw).citations if c.valid for k in c.keys}
        previous = 0
        output_cursor = 0
        for edit in result.edits:
            self.assertIn((edit.start, edit.end), allowed)
            self.assertGreaterEqual(edit.start, previous)
            self.assertEqual(raw[edit.start:edit.end], edit.old_key)
            unchanged = raw[previous:edit.start]
            self.assertEqual(result.content[output_cursor:output_cursor + len(unchanged)], unchanged)
            output_cursor += len(unchanged)
            self.assertEqual(result.content[output_cursor:output_cursor + len(edit.new_key)], edit.new_key)
            output_cursor += len(edit.new_key)
            previous = edit.end
        self.assertEqual(result.content[output_cursor:], raw[previous:])
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_empty_and_plain_text_are_unchanged(self):
        for raw in ('', 'foo and bar.', 'Nguyễn và cộng sự.'):
            self.assertEqual(self.remap(raw).content, raw)
            self.assertFalse(self.scan(raw).citations)

    def test_single_and_multikey_citations_preserve_all_other_characters(self):
        raw = r'foo: \cite{foo, bar,foo} and bar.'
        result = self.remap(raw)
        self.assertEqual(result.content, r'foo: \cite{ref_a, ref_b,ref_a} and bar.')
        self.assertEqual(len(result.edits), 3)

    def test_star_and_nested_optional_arguments_are_preserved(self):
        raw = r'\citep*[\emph{see [A]}][Section 2]{ foo ,bar }'
        result = self.remap(raw)
        self.assertEqual(result.content, r'\citep*[\emph{see [A]}][Section 2]{ ref_a ,ref_b }')
        citation = result.citations[0]
        self.assertTrue(citation.starred)
        self.assertEqual(citation.command, 'citep')
        self.assertEqual(len(citation.optional_arguments), 2)

    def test_comments_between_arguments_and_multiline_key_list(self):
        raw = '\\citet % header\n [see] % note\n [p. 2]\n { foo,\n bar }'
        self.assertEqual(self.remap(raw).content,
                         '\\citet % header\n [see] % note\n [p. 2]\n { ref_a,\n ref_b }')

    def test_command_families(self):
        for name in ('cite', 'citep', 'citet', 'citealp', 'citealt', 'citeauthor', 'citeyear',
                     'citeyearpar', 'Citep', 'Citet', 'parencite', 'textcite', 'autocite',
                     'footcite', 'footcitetext', 'supercite', 'nocite'):
            raw = '\\' + name + '{foo}'
            with self.subTest(name=name):
                self.assertEqual(self.remap(raw).content, '\\' + name + '{ref_a}')

    def test_unknown_key_is_preserved_with_exact_warning(self):
        raw = r'\cite{foo,missing}'
        result = self.remap(raw)
        self.assertEqual(result.content, r'\cite{ref_a,missing}')
        warning = next(w for w in result.warnings if w.code == 'unknown_citation_key')
        self.assertEqual(raw[warning.start:warning.end], 'missing')
        self.assertEqual([d.status for d in result.decisions], ['remapped', 'unresolved'])

    def test_none_mapping_marks_upstream_ambiguity(self):
        result = self.remap(r'\cite{foo,bar}', mapping(foo=None, bar='ref_b'))
        self.assertEqual(result.content, r'\cite{foo,ref_b}')
        self.assertIn('ambiguous_citation_mapping', [w.code for w in result.warnings])

    def test_version_scope_same_old_key_can_have_different_meanings(self):
        given = {('p', 1, 'foo'): 'paper_a', ('p', 2, 'foo'): 'paper_b'}
        self.assertEqual(self.remap(r'\cite{foo}', given, version=1).content, r'\cite{paper_a}')
        self.assertEqual(self.remap(r'\cite{foo}', given, version=2).content, r'\cite{paper_b}')

    def test_publication_scope_does_not_leak(self):
        given = {('p', 1, 'foo'): 'paper_a', ('other', 1, 'foo'): 'paper_b'}
        self.assertEqual(self.remap(r'\cite{foo}', given, pub='other').content, r'\cite{paper_b}')

    def test_identity_mapping_is_audited_without_edit(self):
        result = self.remap(r'\cite{foo}', mapping(foo='foo'))
        self.assertFalse(result.edits)
        self.assertEqual(result.decisions[0].status, 'unchanged')

    def test_nocite_wildcard_and_mixed_wildcard(self):
        wildcard = self.remap(r'\nocite{ * }')
        self.assertFalse(wildcard.edits)
        self.assertFalse(wildcard.warnings)
        self.assertEqual(wildcard.decisions[0].status, 'wildcard')
        mixed = self.remap(r'\nocite{*,foo}')
        self.assertEqual(mixed.content, mixed.raw)
        self.assertIn('mixed_nocite_wildcard', [w.code for w in mixed.warnings])

    def test_empty_dynamic_commented_or_malformed_keys_preserve_entire_command(self):
        for raw in (r'\cite{}', r'\cite{foo,}', r'\cite{foo,\macro}', r'\cite{{foo}}',
                    '\\cite{foo,%comment\nbar}', r'\cite{foo bar}', r'\cite{foo$bar}'):
            with self.subTest(raw=raw):
                result = self.remap(raw)
                self.assertEqual(result.content, raw)
                self.assertFalse(result.citations[0].valid)
                self.assertTrue(result.warnings)

    def test_more_than_two_options_are_deferred(self):
        raw = r'\cite[a][b][c]{foo}'
        result = self.remap(raw)
        self.assertEqual(result.content, raw)
        self.assertIn('citation_options_deferred', [w.code for w in result.warnings])

    def test_nested_cite_inside_citation_option_is_not_rewritten(self):
        raw = r'\citep[see \cite{bar}]{foo}'
        result = self.remap(raw)
        self.assertEqual(result.content, r'\citep[see \cite{bar}]{ref_a}')
        self.assertIn('citation_in_option_deferred', [w.code for w in result.warnings])

    def test_known_text_wrappers_and_adjacent_groups(self):
        raw = r'\textbf{See \emph{\cite{foo}}}{\cite{bar}}'
        self.assertEqual(self.remap(raw).content, r'\textbf{See \emph{\cite{ref_a}}}{\cite{ref_b}}')

    def test_caption_and_table_cell_citations(self):
        raw = r'\begin{figure}\caption[Short \cite{foo}]{Long \cite{bar}}\end{figure}'
        raw += r'\begin{table}\begin{tabular}{cc}A & \cite{foo} \\\end{tabular}\end{table}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 3)
        self.assertIn(r'\caption[Short \cite{ref_a}]{Long \cite{ref_b}}', result.content)

    def test_href_display_is_scanned_target_and_url_commands_are_preserved(self):
        raw = (r'\url{https://example.org/\cite{foo}} \path{\cite{foo}} '
               r'\href{https://example.org/a%20b/\cite{foo}}{See \cite{bar}}')
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn(r'{See \cite{ref_b}}', result.content)

    def test_bare_url_is_not_scanned_as_latex(self):
        raw = r'https://example.org/\cite{foo} \cite{bar}'
        self.assertEqual(self.remap(raw).content, r'https://example.org/\cite{foo} \cite{ref_b}')

    def test_prose_labels_and_reference_keys_are_not_substring_replaced(self):
        raw = r'foo \label{foo}\ref{foo}\eqref{foo} \cite{foo}'
        self.assertEqual(self.remap(raw).content, r'foo \label{foo}\ref{foo}\eqref{foo} \cite{ref_a}')

    def test_comments_escaped_percent_and_escaped_backslash(self):
        raw = '% \\cite{foo}\n' + r'\% actual \cite{foo} \\cite{bar}'
        result = self.remap(raw)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(len(result.edits), 1)
        self.assertIn(r'\\cite{bar}', result.content)

    def test_verbatim_code_and_comment_environments_are_not_changed(self):
        raw = (r'\verb|\cite{foo}|\verb*+\cite{foo}+'
               r'\begin{verbatim}\cite{foo}\end{verbatim}'
               r'\begin{minted}{tex}\cite{foo}\end{minted}'
               r'\begin{comment}\cite{foo}\end{comment} \cite{bar}')
        result = self.remap(raw)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.content, raw[:-len(r'\cite{bar}')] + r'\cite{ref_b}')

    def test_math_citations_are_deferred_with_warning(self):
        raw = r'$\cite{foo}$ \begin{align}x&=\cite{foo}\end{align} \cite{bar}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn('citation_in_math_deferred', [w.code for w in result.warnings])

    def test_bibliography_body_not_remapped(self):
        raw = r'\cite{foo}\begin{thebibliography}{1}\bibitem{foo}See \cite{bar}\end{thebibliography}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertTrue(result.content.endswith(r'\bibitem{foo}See \cite{bar}\end{thebibliography}'))
        self.assertIn('citation_environment_deferred', [w.code for w in result.warnings])

    def test_macros_and_unknown_arguments_are_not_expanded(self):
        raw = (r'\newcommand{\myref}{\cite{foo}}\def\myother#1{\cite{#1}}'
               r'\custom{\cite{bar}} \cite{foo}')
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn('citation_macro_definition_deferred', [w.code for w in result.warnings])
        self.assertIn('citation_command_arguments_deferred', [w.code for w in result.warnings])

    def test_unsupported_multicite_and_alias_commands_are_deferred(self):
        raw = r'\cites{foo}{bar} \mycite{foo} \cite{bar}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn('unsupported_citation_command', [w.code for w in result.warnings])

    def test_conditionals_and_alias_declarations(self):
        raw = r'\newif\ifdraft\let\mycite=\cite \iffalse\cite{foo}\else\cite{bar}\fi \cite{foo}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn('citation_conditional_deferred', [w.code for w in result.warnings])
        self.assertIn('citation_alias_deferred', [w.code for w in result.warnings])

    def test_unknown_environment_with_literal_cite_is_deferred(self):
        raw = r'\begin{custom}\cite{foo}\end{custom} \cite{bar}'
        result = self.remap(raw)
        self.assertEqual(len(result.edits), 1)
        self.assertIn('citation_environment_deferred', [w.code for w in result.warnings])

    def test_unclosed_constructs_do_not_guess_recovery(self):
        for tail in (r'\cite[unclosed', r'\cite{foo', r'\verb|\cite{foo}',
                     r'\begin{custom}\cite{foo}', r'$\cite{foo}', r'{\cite{foo}',
                     r'\href{https://example.org}{\cite{foo}'):
            raw = r'\cite{bar} ' + tail
            result = self.remap(raw)
            self.assertEqual(len(result.edits), 1)
            self.assertTrue(result.warnings)

    def test_missing_key_argument_does_not_suppress_following_valid_command(self):
        raw = r'\cite unbraced \cite{foo}'
        result = self.remap(raw)
        self.assertEqual(result.content, r'\cite unbraced \cite{ref_a}')
        self.assertIn('missing_citation_argument', [w.code for w in result.warnings])

    def test_depth_limit(self):
        raw = '{' * 70 + r'\cite{foo}' + '}' * 70
        result = self.remap(raw)
        self.assertEqual(result.content, raw)
        self.assertIn('citation_depth_limit', [w.code for w in result.warnings])

    def test_unicode_and_crlf_offsets(self):
        raw = 'Nguyễn\r\n\\cite{foo}'
        result = self.remap(raw)
        self.assertEqual(result.edits[0].start, len('Nguyễn\r\n\\cite{'))
        self.assertEqual(result.content, 'Nguyễn\r\n\\cite{ref_a}')

    def test_invalid_mapping_and_context_fail_before_edit(self):
        for given in ({'foo': 'ref_a'}, {('p', True, 'foo'): 'a'}, mapping(foo='a,b'),
                      mapping(foo=r'bad\macro'), mapping(foo='*'), mapping(foo=''), mapping(foo='bad#1')):
            with self.assertRaises(ValueError):
                self.remap(r'\cite{foo}', given)
        with self.assertRaises(ValueError):
            self.remap(r'\cite{foo}', version=True)
        with self.assertRaises(TypeError):
            remap_citations(None, {}, arxiv_id='p', version=1)

    def test_chains_cycles_and_ambiguous_targets_rejected(self):
        for given in (mapping(foo='bar', bar='baz'), mapping(foo='bar', bar='foo'),
                      mapping(foo='bar', bar=None)):
            with self.assertRaises(ValueError):
                self.remap(r'\cite{foo}', given)

    def test_canonical_mapping_content_is_stable_on_reapplication(self):
        given = mapping(foo='ref_a', ref_a='ref_a')
        first = self.remap(r'\cite{foo}', given)
        second = self.remap(first.content, given)
        self.assertEqual(first.content, second.content)
        self.assertFalse(second.edits)

    def test_frozen_results_and_json_serialization(self):
        result = self.remap(r'\cite{foo}')
        with self.assertRaises(FrozenInstanceError):
            result.content = 'changed'
        self.assertEqual(json.loads(json.dumps(asdict(result)))['edits'][0]['new_key'], 'ref_a')



    def test_seeded_mixed_inputs_and_final_target_stability(self):
        rng = random.Random(20261104)
        chunks = [r'\cite{foo, bar}', r'\citep[see][p.2]{foo}', r'\textbf{\cite{bar}}',
                  '% \\cite{foo}\n', r'\verb|\cite{foo}|', r'\custom{\cite{foo}}',
                  r'\href{https://example.org}{\cite{bar}}', r'\nocite{*}', 'Nguyễn ',
                  r'$\cite{foo}$']
        given = mapping(foo='ref_a', bar='ref_b', ref_a='ref_a', ref_b='ref_b')
        for _ in range(250):
            raw = ' '.join(rng.choices(chunks, k=8))
            result = self.remap(raw, given)
            self.assertEqual(remap_citations(result.content, given, arxiv_id='p', version=1).content, result.content)


if __name__ == '__main__':
    unittest.main()
