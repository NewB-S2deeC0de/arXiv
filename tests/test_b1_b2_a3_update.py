"""A3 handoff, conservative figure cleanup and external declaration inspection."""
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace as Obj
import unittest

from standardization import (normalize_figure, normalize_a3_hierarchy,
    inspect_bibliography_dependencies, extract_parser_bibliography,
    source_map_from_spans, RawSourceMap, SourceMapPiece, MappedSourceSpan,
    inspect_version_bibliography)


@dataclass
class Node:
    occurrence_id: int
    kind: str
    raw: str
    source_spans: list = field(default_factory=list)
    children: list = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    content: str | None = None
    warnings: list = field(default_factory=list)


def tree(*children, status='ready'):
    return Obj(arxiv_id='fixture-only', version=1, status=status,
               root=Node(100, 'document', '', children=list(children)),
               bibliography=[], diagnostics=[])


class FigureCleanupTests(unittest.TestCase):
    def check(self, raw):
        result = normalize_figure(raw)
        self.assertEqual(normalize_figure(result.content).content, result.content)
        for warning in result.warnings:
            self.assertTrue(0 <= warning.start <= warning.end <= len(raw))
        return result

    def test_placement_centering_and_caption_label_paths_preserved(self):
        raw = r'\begin{figure}[htbp!]\centering\includegraphics[width=.5\textwidth]{a.pdf}\caption{Text \cite{k}}\label{fig:x}\end{figure}'
        result = self.check(raw)
        self.assertEqual(result.content, raw.replace('[htbp!]', '').replace(r'\centering', ''))
        self.assertEqual(result.content.count(r'\caption'), 1)

    def test_table_structure_math_and_citations_are_unchanged(self):
        raw = r'\begin{table}[t]\centering\begin{tabular}{cc}A&B\\\midrule $x+y$&\cite{k}\\\multicolumn{2}{c}{X}\end{tabular}\caption{Table}\end{table}'
        result = self.check(raw)
        self.assertEqual(result.content, raw.replace('[t]', '').replace(r'\centering', '').replace(r'\midrule', ''))

    def test_commands_inside_caption_math_comment_and_code_not_removed(self):
        raw = ('\\begin{figure}\\centering\n% \\centering\n' +
               r'\caption{Literal \centering and \midrule} $\centering$'
               r'\begin{verbatim}\centering\end{verbatim}\end{figure}')
        result = self.check(raw)
        self.assertEqual(result.content, raw.replace(r'\centering', '', 1))

    def test_fake_end_inside_definition_argument_or_option_is_protected(self):
        for inner in (r'\newcommand{\fake}{\end{figure}}',
                      r'\custom[meta={\end{figure}}]{x}', r'\href{url}{\end{figure}}'):
            raw = r'\begin{figure}\centering' + inner + r'\caption{Real}\end{figure}'
            self.assertEqual(self.check(raw).content, raw.replace(r'\centering', ''))

    def test_unknown_environment_and_optional_midrule_preserved(self):
        raw = r'\begin{figure}\begin{custom}\centering\end{custom}\midrule[2pt]\end{figure}'
        result = self.check(raw)
        self.assertEqual(result.content, raw)
        self.assertTrue(result.warnings)

    def test_malformed_or_unsafe_boundaries_preserve_whole_raw(self):
        for raw in (r'\begin{figure}\centering\caption{Bad}',
                    r'\begin{figure}\foo\centering{body}\end{figure}',
                    r'\begin{figure}\centering\end{table}'):
            result = self.check(raw)
            self.assertEqual(result.content, raw)
            self.assertEqual(result.warnings[0].code, 'figure_cleanup_deferred')

    def test_algorithm_and_nonstandard_placement_are_deferred(self):
        for raw in (r'\begin{algorithm}\centering\caption{Code}\end{algorithm}',
                    r'\begin{figure}[width=1]\caption{X}\end{figure}'):
            result = self.check(raw)
            self.assertEqual(result.content, raw)
            self.assertTrue(result.warnings)


class A3NormalizationTests(unittest.TestCase):
    def test_content_filled_on_copy_with_attrs_raw_order_and_bibliography_intact(self):
        sentence = Node(1, 'sentence', r'Use \textbf{this} \(x+y\) and \cite{k}.')
        formula = Node(2, 'block_formula', r'\[x+y\]', attrs={'environment': r'\['})
        figure = Node(3, 'figure', r'\begin{figure}[t]\centering\caption{Once}\end{figure}',
                      attrs={'subtype': 'figure', 'caption': 'Once', 'label': 'fig:x'})
        parent = Node(4, 'paragraph', sentence.raw, children=[sentence])
        source = tree(parent, formula, figure)
        source.bibliography = [Obj(raw=r'\bibliography{refs}', source_spans=[])]
        before = deepcopy(source)
        result = normalize_a3_hierarchy(source)
        self.assertEqual(source, before)
        self.assertEqual([n.occurrence_id for n in result.hierarchy.root.children], [4, 2, 3])
        self.assertIsNone(result.hierarchy.root.children[0].content)
        self.assertEqual(result.hierarchy.root.children[0].children[0].content, r'Use this $x+y$ and \cite{k}.')
        self.assertEqual(result.hierarchy.root.children[1].content, r'\begin{equation*}x+y\end{equation*}')
        self.assertEqual(result.hierarchy.root.children[2].content.count('Once'), 1)
        self.assertEqual(result.hierarchy.root.children[2].attrs, figure.attrs)
        self.assertEqual(result.hierarchy.bibliography, source.bibliography)

    def test_partial_unsplit_paragraph_kept_with_diagnostic(self):
        source = tree(Node(1, 'paragraph', r'\begin{minted}{tex}Raw', warnings=['unsupported_literal']), status='partial')
        result = normalize_a3_hierarchy(source)
        self.assertEqual(result.hierarchy.status, 'partial')
        self.assertEqual(result.hierarchy.root.children[0].content, source.root.children[0].raw)
        self.assertEqual(result.diagnostics[0].code, 'a3_paragraph_unresolved')

    def test_stale_source_text_preserves_leaf_and_reports_mapping_failure(self):
        source = tree(Node(1, 'sentence', r'\textbf{X}', source_spans=[Obj(path='x', start=0, end=10)]))
        result = normalize_a3_hierarchy(source, source_texts={'x': 'different!'})
        self.assertEqual(result.source_mapping_failures, 1)
        self.assertEqual(result.hierarchy.root.children[0].content, source.root.children[0].raw)

    def test_unparseable_and_duplicate_occurrences_are_not_false_success(self):
        source = tree(status='unparseable');source.root = None
        self.assertIsNone(normalize_a3_hierarchy(source).hierarchy.root)
        with self.assertRaises(ValueError):
            normalize_a3_hierarchy(tree(Node(1, 'sentence', 'A'), Node(1, 'sentence', 'B')))


class BibliographyDependencyTests(unittest.TestCase):
    def test_bibliography_multi_target_and_addbibresource_options(self):
        raw = r'\bibliography{ refs, second.bib }\addbibresource[encoding=utf8]{third.bib}'
        result = inspect_bibliography_dependencies(raw, listed_bib_files=['refs.bib', 'second.bib', 'third.bib'])
        self.assertEqual([d.command for d in result.declarations], ['bibliography', 'addbibresource'])
        self.assertEqual([t.listed_match for d in result.declarations for t in d.targets],
                         ['refs.bib', 'second.bib', 'third.bib'])
        for d in result.declarations:
            self.assertEqual(raw[d.start:d.end], d.raw)
            for target in d.targets:
                self.assertEqual(raw[target.start:target.end], target.raw)

    def test_comment_macro_and_opaque_argument_do_not_create_dependencies(self):
        raw = ('% \\bibliography{fake}\n' + r'\newcommand{\x}{\bibliography{fake}}'
               r'\custom{\bibliography{fake}}\verb|\bibliography{fake}|\bibliography{real}')
        result = inspect_bibliography_dependencies(raw, listed_bib_files=['real.bib'])
        self.assertEqual(len(result.declarations), 1)
        self.assertEqual(result.declarations[0].targets[0].listed_match, 'real.bib')

    def test_source_relative_and_root_relative_ambiguity_is_not_resolved_silently(self):
        raw = r'\bibliography{refs}'
        mapping = RawSourceMap(raw, (SourceMapPiece(0, len(raw), MappedSourceSpan('parts/body.tex', 0, len(raw))),))
        result = inspect_bibliography_dependencies(raw, source_map=mapping, root_file='main.tex',
                                                    listed_bib_files=['refs.bib', 'parts/refs.bib'])
        target = result.declarations[0].targets[0]
        self.assertIsNone(target.listed_match)
        self.assertEqual(target.candidate_paths, ('parts/refs.bib', 'refs.bib'))

    def test_print_boundary_does_not_guess_resources(self):
        result = inspect_bibliography_dependencies(r'\printbibliography[title={Refs}]', listed_bib_files=['refs.bib'])
        self.assertEqual(result.declarations[0].targets, ())
        self.assertEqual(result.warnings[0].code, 'bibliography_resources_require_context')

    def test_dynamic_missing_external_and_escaping_targets_are_reported(self):
        for raw, status in ((r'\bibliography{\jobname}', 'dynamic_or_unsupported_target'),
                            (r'\bibliography{missing}', 'not_in_listed_bib_files'),
                            (r'\bibliography{../outside}', 'unsafe_or_external_target'),
                            (r'\addbibresource{https://example.org/refs.bib}', 'unsafe_or_external_target')):
            result = inspect_bibliography_dependencies(raw)
            self.assertEqual(result.declarations[0].targets[0].status, status)
            self.assertTrue(result.warnings)

    def test_adapter_classifies_bibitem_external_and_mixed_blocks(self):
        bib = r'\begin{thebibliography}{1}\bibitem{k}Body.\end{thebibliography}'
        for raw, kind in ((bib, 'bibitems'), (r'\bibliography{refs}', 'external'),
                          (bib + r'\bibliography{refs}', 'mixed'), ('Unknown', 'unresolved')):
            result = extract_parser_bibliography(Obj(raw=raw, source_spans=[]), arxiv_id='p', version=1,
                                                 listed_bib_files=['refs.bib'])
            self.assertEqual(result.block_kind, kind)

    def test_malformed_declaration_does_not_silently_look_like_no_dependencies(self):
        for raw in (r'\bibliography{refs', r'\addbibresource[encoding=utf8', r'\bibliography'):
            result = inspect_bibliography_dependencies(raw)
            self.assertTrue(result.warnings)
            self.assertEqual(result.warnings[0].code, 'bibliography_dependency_malformed')






if __name__ == '__main__':
    unittest.main()
