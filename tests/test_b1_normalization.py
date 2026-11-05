"""B1 scanner/math regression tests; runnable independently of M1 deps."""

from dataclasses import FrozenInstanceError
import random
import unittest

from standardization import (
    normalize_block_math, normalize_inline_math, normalize_text, scan_latex,
)


class ScannerTests(unittest.TestCase):
    def assert_lossless(self, raw):
        result = scan_latex(raw)
        self.assertEqual(''.join(s.raw for s in result.segments), raw)
        position = 0
        for segment in result.segments:
            self.assertEqual(segment.start, position)
            self.assertGreater(segment.end, segment.start)
            self.assertEqual(segment.raw, raw[segment.start:segment.end])
            position = segment.end
        self.assertEqual(position, len(raw))
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_empty_and_plain_unicode(self):
        for raw in ('', 'A simple sentence.', 'Công thức và tác giả Việt Nam.'):
            with self.subTest(raw=raw):
                result = self.assert_lossless(raw)
                self.assertFalse(result.warnings)

    def test_mixed_paragraph_regions(self):
        raw = r'We use \textbf{our method} with \(x+y\) and \cite{foo}.'
        result = self.assert_lossless(raw)
        self.assertEqual([s.kind for s in result.segments], [
            'text', 'formatting', 'text', 'inline_math', 'text', 'citation', 'text',
        ])

    def test_nested_formatting_is_one_protected_segment(self):
        raw = r'\textbf{Our \emph{new} method}'
        result = self.assert_lossless(raw)
        self.assertEqual([(s.kind, s.raw) for s in result.segments], [('formatting', raw)])
        self.assertFalse(result.warnings)

    def test_citation_optional_arguments_and_multiple_keys(self):
        raw = r'\citep*[see {Eq. 2}][pp. 3--4]{foo,bar}'
        result = self.assert_lossless(raw)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].kind, 'citation')

    def test_citation_arguments_can_wrap_lines(self):
        raw = '\\cite\n[see]\n{foo,bar}'
        self.assertEqual(self.assert_lossless(raw).segments[0].raw, raw)

    def test_url_commands_and_bare_url(self):
        for raw in (r'\url{https://a.org/x_y?q=10%20}',
                    r'\href{https://a.org/x_y}{An \emph{example}}',
                    'https://a.org/x_y?q=1.2'):
            with self.subTest(raw=raw):
                result = self.assert_lossless(raw)
                self.assertEqual(result.segments[0].kind, 'url')
                self.assertFalse(result.warnings)

    def test_escaped_url_percent(self):
        raw = r'\url{https://a.org/x_y?q=10\%20}'
        result = self.assert_lossless(raw)
        self.assertEqual(result.segments[0].kind, 'url')
        self.assertFalse(result.warnings)

    def test_escaped_special_characters_are_not_delimiters(self):
        raw = r'The price is \$5 and rate is 10\%; use a\_b and \{x\}.'
        result = self.assert_lossless(raw)
        self.assertFalse(result.warnings)
        self.assertNotIn('inline_math', [s.kind for s in result.segments])

    def test_math_delimiters(self):
        for raw, kind in ((r'$x+y$', 'inline_math'), (r'\(x+y\)', 'inline_math'),
                          (r'$$x+y$$', 'block_math'), (r'\[x+y\]', 'block_math')):
            with self.subTest(raw=raw):
                result = self.assert_lossless(raw)
                self.assertEqual(result.segments[0].kind, kind)
                self.assertFalse(result.warnings)

    def test_math_escaped_dollar_and_nested_braces(self):
        raw = r'$\text{cost \$5} + x_{i_{j}}$'
        result = self.assert_lossless(raw)
        self.assertEqual(result.segments[0].raw, raw)
        self.assertFalse(result.warnings)

    def test_nested_math_environments_and_labels(self):
        raw = r'\begin{equation}\label{eq:a}\begin{aligned}x&=1\\y&=2\end{aligned}\end{equation}'
        result = self.assert_lossless(raw)
        self.assertEqual(result.segments[0].kind, 'block_math')
        self.assertFalse(result.warnings)

    def test_figure_and_table_are_not_flattened(self):
        for name in ('figure', 'table'):
            raw = ('\\begin{' + name + '}[htpb]\n\\centering\n'
                   '\\caption{Use $x$ and \\cite{foo}.}\\label{fig:a}\n'
                   '\\begin{tabular}{cc}a&b\\\\\\midrule c&d\\\\\\end{tabular}\n'
                   '\\end{' + name + '}')
            with self.subTest(name=name):
                result = self.assert_lossless(raw)
                self.assertEqual(result.segments[0].kind, 'environment')
                self.assertEqual(len(result.segments), 1)
                self.assertFalse(result.warnings)

    def test_inline_verbatim_with_latex_looking_contents(self):
        for raw in (r'\verb|$x$ \cite{fake} % {|', r'\verb*+\(x\)+'):
            with self.subTest(raw=raw):
                result = self.assert_lossless(raw)
                self.assertEqual(result.segments[0].kind, 'verbatim')
                self.assertFalse(result.warnings)

    def test_verbatim_environment(self):
        raw = '\\begin{verbatim}\n$x$ % \\cite{fake}\n\\end{verbatim}'
        result = self.assert_lossless(raw)
        self.assertEqual(result.segments[0].kind, 'verbatim')
        self.assertFalse(result.warnings)

    def test_comment_does_not_expose_fake_commands(self):
        raw = 'Text % \\cite{fake} $unclosed\nNext.'
        result = self.assert_lossless(raw)
        self.assertEqual([s.kind for s in result.segments], ['text', 'comment', 'text'])
        self.assertFalse(result.warnings)

    def test_comment_inside_brace_group(self):
        raw = '\\textbf{a% } ignored\nb}'
        result = self.assert_lossless(raw)
        self.assertEqual(result.segments[0].kind, 'formatting')
        self.assertFalse(result.warnings)

    def test_unknown_macro_and_standalone_group_preserved(self):
        raw = r'\custom[option]{a{b}c} and {\bf old style}'
        result = self.assert_lossless(raw)
        self.assertEqual([s.kind for s in result.segments], ['command', 'text', 'group'])

    def test_unclosed_constructs_retain_remaining_input(self):
        for raw, code in ((r'Text \(x+y', 'unclosed_math'),
                          (r'$x+y', 'unclosed_math'),
                          (r'\textbf{missing', 'unclosed_construct'),
                          (r'{missing', 'unclosed_group'),
                          (r'\verb|missing', 'unclosed_verbatim'),
                          (r'\begin{equation}x', 'unclosed_environment'),
                          ('Text \\', 'dangling_backslash')):
            with self.subTest(raw=raw):
                result = self.assert_lossless(raw)
                self.assertEqual(result.segments[-1].kind, 'malformed')
                self.assertEqual(result.warnings[-1].code, code)

    def test_mismatched_environments_are_not_guessed(self):
        raw = r'\begin{figure}text\end{table}'
        self.assertTrue(self.assert_lossless(raw).warnings)

    def test_unmatched_closing_brace_warns(self):
        self.assertEqual(self.assert_lossless('x}y').warnings[0].code, 'unexpected_closing_brace')

    def test_stray_math_closer_warns(self):
        self.assertEqual(self.assert_lossless(r'x\)y').warnings[0].code, 'unexpected_math_closer')

    def test_known_command_does_not_consume_following_group(self):
        result = self.assert_lossless(r'\cite{foo} {another group}')
        self.assertEqual([s.kind for s in result.segments], ['citation', 'text', 'group'])

    def test_scanner_rejects_non_string_input(self):
        with self.assertRaises(TypeError):
            scan_latex(None)

    def test_scanner_deterministic_losslessness_on_mixed_inputs(self):
        randomizer = random.Random(42)
        atoms = ['text', ' Việt ', r'$x$', r'\(x\)', r'\cite{foo}', r'\textbf{a}',
                 r'\verb|$|', '\n', '{', '}', '$', '\\', '% comment\n']
        for _ in range(250):
            raw = ''.join(randomizer.choices(atoms, k=randomizer.randrange(1, 15)))
            self.assert_lossless(raw)


class NormalizationTests(unittest.TestCase):
    def test_plain_text_whitespace_is_normalized_in_stage_two(self):
        raw = 'Some  spaces\nand another line.\n\nA paragraph.'
        self.assertEqual(normalize_text(raw).content, 'Some spaces and another line.\n\nA paragraph.')

    def test_inline_math_canonicalization(self):
        self.assertEqual(normalize_inline_math(r'\(x+y\)').content, '$x+y$')
        self.assertEqual(normalize_inline_math('$x+y$').content, '$x+y$')

    def test_empty_inline_math_does_not_turn_into_display_delimiter(self):
        raw = r'\(\) text \(\)'
        result = normalize_text(raw)
        self.assertEqual(result.content, raw)
        self.assertEqual(normalize_inline_math(r'\(\)').content, r'\(\)')
        self.assertEqual(normalize_text(result.content).content, result.content)
        self.assertIn('empty_inline_math_deferred', [w.code for w in result.warnings])

    def test_block_math_canonicalization(self):
        expected = r'\begin{equation*}x+y\end{equation*}'
        for raw in (r'\[x+y\]', '$$x+y$$', expected):
            with self.subTest(raw=raw):
                result = normalize_block_math(raw)
                self.assertEqual(result.content, expected)
                self.assertFalse(result.warnings)

    def test_mixed_text_normalizes_math_but_preserves_citation(self):
        raw = r'We use \(x+y\) and \cite[see]{foo,bar}.'
        result = normalize_text(raw)
        self.assertEqual(result.content, r'We use $x+y$ and \cite[see]{foo,bar}.')
        self.assertFalse(result.warnings)

    def test_nested_formatting_is_cleaned_with_inline_math(self):
        raw = r'\textbf{Our \emph{new} method and \(x\)}'
        result = normalize_text(raw)
        self.assertEqual(result.content, 'Our new method and $x$')
        self.assertFalse(result.warnings)

    def test_complex_and_starred_math_preserve_structure(self):
        for raw in (r'\begin{align}a&=b\\c&=d\end{align}',
                    r'\begin{equation*}x\label{eq:a}\end{equation*}'):
            with self.subTest(raw=raw):
                result = normalize_block_math(raw)
                self.assertEqual(result.content, raw)
                self.assertFalse(result.warnings)

    def test_labels_retained_in_simple_math(self):
        raw = r'\[x\label{eq:a}\]'
        self.assertEqual(normalize_block_math(raw).content,
                         r'\begin{equation*}x\label{eq:a}\end{equation*}')

    def test_wrong_math_region_preserved_and_warned(self):
        for function, raw in ((normalize_inline_math, 'prefix $x$'),
                              (normalize_block_math, '$x$'),
                              (normalize_inline_math, '')):
            with self.subTest(raw=raw):
                result = function(raw)
                self.assertEqual(result.content, raw)
                self.assertEqual(result.warnings[0].code, 'unexpected_region')

    def test_malformed_math_does_not_drop_tail(self):
        raw = r'prefix \(x+y and \cite{foo}'
        result = normalize_text(raw)
        self.assertEqual(result.content, raw)
        self.assertEqual(result.warnings[0].code, 'unclosed_math')

    def test_code_url_and_figure_preserved(self):
        for raw in (r'\verb|\(x\)|', r'\url{https://a.org/x_y}',
                    r'\begin{figure}[htpb]\caption{\(x\)}\end{figure}'):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_text(raw).content, raw)

    def test_adjacent_inline_math_does_not_create_double_dollar(self):
        for raw in (r'\(x\)\(y\)', r'$x$\(y\)', r'\(x\)$y$'):
            with self.subTest(raw=raw):
                result = normalize_text(raw)
                self.assertEqual(result.content, raw)
                self.assertIn('adjacent_math_deferred', [w.code for w in result.warnings])
                self.assertEqual(normalize_text(result.content).content, result.content)

    def test_diagnostics_use_original_unicode_character_offsets(self):
        raw = r'Việt \texttt{x}'
        warning = normalize_text(raw).warnings[0]
        self.assertEqual(raw[warning.start:warning.end], r'\texttt{x}')

    def test_result_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            normalize_text('x').content = 'y'

    def test_idempotence_for_supported_and_deferred_fixtures(self):
        fixtures = [
            '', 'Plain  text.', r'Use \(x\) and \cite{foo}.', r'\[x+y\]',
            r'\textbf{Our \emph{new} method}', r'\verb|$x$|',
            r'\begin{align}x&=1\\y&=2\end{align}', r'\label{eq:a}',
            r'broken \(x', r'https://a.org/x_y?q=0.5',
        ]
        for raw in fixtures:
            with self.subTest(raw=raw):
                first = normalize_text(raw)
                self.assertEqual(normalize_text(first.content).content, first.content)

    def test_idempotence_on_deterministic_mixed_fixtures(self):
        randomizer = random.Random(17)
        atoms = ['a', ' Việt ', r'\(x\)', '$y$', r'\[z\]', r'\cite{foo}',
                 r'\verb|$|', r'\emph{x}', r'\url{https://a.org/x_y}',
                 ' ', r'\(\)', '$', '{', '}', '\n']
        for _ in range(1000):
            raw = ''.join(randomizer.choices(atoms, k=randomizer.randrange(1, 12)))
            once = normalize_text(raw).content
            self.assertEqual(normalize_text(once).content, once, repr(raw))


if __name__ == '__main__':
    unittest.main()
