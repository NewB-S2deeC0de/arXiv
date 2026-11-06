"""Acceptance tests for the approved B1 stage 2 prose cleanup rules."""

from copy import deepcopy
import random
import unittest

from standardization import normalize_text


class ProseCleanupTests(unittest.TestCase):
    def assert_normalized(self, raw, expected):
        result = normalize_text(raw)
        self.assertEqual(result.content, expected)
        self.assertEqual(normalize_text(result.content).content, result.content)
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_plain_sentence_punctuation_and_unicode(self):
        for raw in ('A simple sentence.', 'Nguyễn dùng phương pháp mới.',
                    'et al. use 0.5, i.e. a threshold.', 'pre-\ntrained'):
            expected = raw.replace('\n', ' ')
            with self.subTest(raw=raw):
                self.assert_normalized(raw, expected)

    def test_allowlisted_formatting_commands(self):
        for name in ('textbf', 'textit', 'emph', 'textrm', 'textsf', 'underline'):
            with self.subTest(name=name):
                result = self.assert_normalized('Our \\' + name + '{method}.', 'Our method.')
                self.assertFalse(result.warnings)

    def test_nested_formatting(self):
        self.assert_normalized(r'\textbf{Our \emph{new} \textit{method}}', 'Our new method')

    def test_user_example(self):
        raw = r'We use \textbf{our \emph{new} method with \(x+y\)} and \cite{foo}.'
        self.assert_normalized(raw, r'We use our new method with $x+y$ and \cite{foo}.')

    def test_no_spaces_invented_at_unwrapped_boundaries(self):
        for raw, expected in ((r'pre\textbf{trained}', 'pretrained'),
                              (r'a\emph{b}c', 'abc'),
                              (r'a\emph{}b', 'ab'),
                              (r'\textbf{A} \emph{B}', 'A B')):
            with self.subTest(raw=raw):
                self.assert_normalized(raw, expected)

    def test_argument_edges_are_not_trimmed_independently(self):
        self.assert_normalized(r'a\emph{ b }c', 'a b c')
        self.assert_normalized(r'a \emph{ b } c', 'a b c')
        self.assert_normalized('  \\emph{ a }  ', 'a')

    def test_whitespace_merges_across_empty_formatting(self):
        self.assert_normalized('a  \\emph{} \t b', 'a b')
        self.assert_normalized('a\n\\emph{}\nb', 'a\n\nb')

    def test_space_tab_and_single_newline(self):
        self.assert_normalized(' \tOur  method\n improves\taccuracy.\t ', 'Our method improves accuracy.')

    def test_paragraph_breaks_and_crlf(self):
        self.assert_normalized(' A\r\n  \r\n\r\nB\rC ', 'A\n\nB C')

    def test_whitespace_only_node_and_empty_formatting(self):
        for raw in ('', ' \t\r\n ', r'\emph{}', r'\textbf{\emph{}}'):
            with self.subTest(raw=raw):
                self.assert_normalized(raw, '')

    def test_nonbreaking_unicode_space_is_not_rewritten(self):
        self.assert_normalized('A\u00a0B', 'A\u00a0B')

    def test_inline_math_body_whitespace_and_formatting_protected(self):
        self.assert_normalized(r'\emph{Use \(x  + \textbf{y}\)}', r'Use $x  + \textbf{y}$')

    def test_block_math_body_is_not_prose_cleaned(self):
        self.assert_normalized('A \\[x  +\ny\\] B',
                               'A \\begin{equation*}x  +\ny\\end{equation*} B')

    def test_citation_inside_formatting_keeps_all_arguments(self):
        raw = '\\textbf{See \\citep[see  also][p.\n2]{foo,bar}}'
        self.assert_normalized(raw, 'See \\citep[see  also][p.\n2]{foo,bar}')

    def test_references_and_labels_preserved_without_warning(self):
        raw = r'\emph{See \ref{sec:a}, \eqref{eq:x} and \label{sec:b}.}'
        result = self.assert_normalized(raw, r'See \ref{sec:a}, \eqref{eq:x} and \label{sec:b}.')
        self.assertFalse(result.warnings)

    def test_url_arguments_and_display_text_are_preserved(self):
        raw = r'\emph{See \href{https://a.org/x_y}{An  \textbf{example}}.}'
        self.assert_normalized(raw, r'See \href{https://a.org/x_y}{An  \textbf{example}}.')
        self.assert_normalized('See  https://a.org/x_y?q=0.5  now.', 'See https://a.org/x_y?q=0.5 now.')

    def test_escapes_are_not_decoded(self):
        raw = r'\emph{Cost \$5, rate 10\%, a\_b, x\&y, \{z\}.}'
        self.assert_normalized(raw, r'Cost \$5, rate 10\%, a\_b, x\&y, \{z\}.')

    def test_texttt_is_kept_code_like(self):
        raw = r'Use \textbf{\texttt{a  \emph{b} \(x\)}}.'
        result = self.assert_normalized(raw, r'Use \texttt{a  \emph{b} \(x\)}.')
        self.assertIn('cleanup_deferred', [w.code for w in result.warnings])

    def test_verbatim_and_code_environment_whitespace_kept(self):
        for protected in (r'\verb|$x$  \textbf{y}|',
                          '\\begin{verbatim}\n  \\emph{x}\n\n\\end{verbatim}'):
            with self.subTest(protected=protected):
                self.assert_normalized(' Use  ' + protected + '  now. ', 'Use ' + protected + ' now.')

    def test_unknown_macros_and_declaration_groups_preserved(self):
        for protected in (r'\custom{a  \textbf{b}}', r'{\bf a  b}', r'{\it a  b}'):
            with self.subTest(protected=protected):
                result = self.assert_normalized('  Use  ' + protected + ' now. ', 'Use ' + protected + ' now.')
                self.assertIn('cleanup_deferred', [w.code for w in result.warnings])

    def test_starred_optional_and_unbraced_formatting_deferred(self):
        for raw in (r'\textbf*{a}', r'\textbf[option]{a}', r'\textbf a'):
            with self.subTest(raw=raw):
                result = self.assert_normalized(raw, raw)
                self.assertIn('cleanup_deferred', [w.code for w in result.warnings])

    def test_figure_and_table_entire_environment_preserved(self):
        for name in ('figure', 'table'):
            raw = ('\\begin{' + name + '}[htpb]\n\\centering\n'
                   '\\caption{A  \\textbf{caption} with \\(x\\).}\\label{a}\n'
                   '\\begin{tabular}{cc}a&b\\\\\\midrule c&d\\\\\\end{tabular}\n'
                   '\\end{' + name + '}')
            with self.subTest(name=name):
                result = self.assert_normalized(raw, raw)
                self.assertIn('cleanup_deferred', [w.code for w in result.warnings])

    def test_comment_newline_kept_so_following_text_is_not_commented_out(self):
        raw = 'A  % \\textbf{not prose}\nB  C'
        result = self.assert_normalized(raw, 'A % \\textbf{not prose}\nB C')
        self.assertIn('cleanup_deferred', [w.code for w in result.warnings])

    def test_comment_inside_supported_formatting(self):
        raw = '\\textbf{a% keep comment\nb}'
        self.assert_normalized(raw, 'a% keep comment\nb')

    def test_nested_warning_offsets_refer_to_original_unicode_raw(self):
        raw = r'Việt \textbf{A \emph{B \custom{c}} D}'
        result = self.assert_normalized(raw, r'Việt A B \custom{c} D')
        warning = next(w for w in result.warnings if w.code == 'cleanup_deferred')
        self.assertEqual(raw[warning.start:warning.end], r'\custom{c}')

    def test_nested_malformed_math_keeps_original_wrapper(self):
        raw = r'\textbf{A \emph{B \(broken}}'
        result = self.assert_normalized(raw, raw)
        warning = next(w for w in result.warnings if w.code == 'unclosed_math')
        self.assertEqual(raw[warning.start:warning.end], r'\(broken')
        self.assertIn('formatting_preserved', [w.code for w in result.warnings])

    def test_missing_brace_keeps_malformed_tail(self):
        raw = r'Prefix \textbf{missing \cite{foo}'
        result = self.assert_normalized(raw, raw)
        self.assertIn('unclosed_construct', [w.code for w in result.warnings])

    def test_depth_limit_does_not_cleanup_more_on_every_call(self):
        raw = r'\textbf{' * 100 + 'x' + '}' * 100
        result = self.assert_normalized(raw, raw)
        self.assertIn('normalization_depth_limit', [w.code for w in result.warnings])

    def test_wrapper_removal_does_not_merge_control_word_names(self):
        for raw in (r'\custom\emph{word}', r'\custom\emph{}word',
                    r'\custom\textbf{{argument}}'):
            with self.subTest(raw=raw):
                result = self.assert_normalized(raw, raw)
                self.assertIn('normalization_boundary_deferred', [w.code for w in result.warnings])

    def test_adjacent_math_across_removed_formatting_keeps_safe_delimiters(self):
        self.assert_normalized(r'\(x\)\emph{}\(y\)', r'\(x\)\(y\)')
        self.assert_normalized(r'\emph{\(x\)}\textbf{\(y\)}', r'\(x\)\(y\)')

    def test_only_callers_output_copy_changes_not_raw_or_tree(self):
        node = {'kind': 'sentence', 'raw': r'\emph{Use \(x\).}',
                'source': {'file': 'main.tex', 'span': [10, 50]}, 'children': []}
        before = deepcopy(node)
        result = normalize_text(node['raw'])
        output = {**node, 'normalized': result.content}
        self.assertEqual(node, before)
        self.assertEqual(output['raw'], before['raw'])
        self.assertEqual(output['normalized'], 'Use $x$.')

    def test_deterministic_generated_nested_fixture_idempotence(self):
        rng = random.Random(2048)
        atoms = ['word', '  text\n', r'\(x  + y\)', r'\cite[see]{a,b}',
                 r'\custom{x}', r'\texttt{x  y}', r'\verb|$|', r'\url{https://a.org/x_y}',
                 '\n\n', r'\(\)', ' Việt ', r'\label{x}']
        for _ in range(1000):
            parts = []
            for _ in range(rng.randrange(1, 8)):
                part = rng.choice(atoms)
                for _ in range(rng.randrange(4)):
                    part = '\\' + rng.choice(('textbf', 'emph', 'textit')) + '{' + part + '}'
                parts.append(part)
            raw = ''.join(parts)
            result = normalize_text(raw)
            self.assertEqual(normalize_text(result.content).content, result.content, repr(raw))
            for warning in result.warnings:
                self.assertTrue(0 <= warning.start <= warning.end <= len(raw))


if __name__ == '__main__':
    unittest.main()
