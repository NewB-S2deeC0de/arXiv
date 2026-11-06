"""B adapters tested against contract-shaped stubs, not the absent A package."""
from dataclasses import dataclass, field
from copy import deepcopy
from pathlib import Path
import unittest

from standardization import normalize_parser_leaf, extract_parser_bibliography


@dataclass
class NodeStub:
    occurrence_id: int
    kind: str
    raw: str
    source_spans: list = field(default_factory=list)
    children: list = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


@dataclass
class BlockStub:
    raw: str
    source_spans: list


class ParserAdapterTests(unittest.TestCase):
    def test_post_a3_prose_returns_content_without_mutating_node(self):
        node = NodeStub(17, 'sentence', r'We \textbf{propose} $x+y$ and \cite{foo}.',
                        [{'opaque_span': 'first'}, {'opaque_span': 'second'}],
                        attrs={'custom': ['keep']}, warnings=['upstream warning'])
        snapshot = deepcopy(node)
        result = normalize_parser_leaf(node, after_a3=True, mode='prose')
        self.assertEqual(result.content, r'We propose $x+y$ and \cite{foo}.')
        self.assertTrue(result.applied)
        self.assertEqual(node, snapshot)
        self.assertEqual(result.occurrence_id, 17)
        self.assertEqual(result.upstream_warnings, ('upstream warning',))
        self.assertEqual(result.raw, node.raw)
        self.assertIs(result.source_spans[0], node.source_spans[0])

    def test_pre_a3_paragraph_is_not_normalized_even_with_prose_mode(self):
        node = NodeStub(2, 'paragraph', r'\textbf{Text}\begin{figure}Content\end{figure}')
        result = normalize_parser_leaf(node, after_a3=False, mode='prose')
        self.assertEqual(result.content, node.raw)
        self.assertFalse(result.applied)
        self.assertEqual(result.diagnostics[0].code, 'normalization_requires_a3')

    def test_structural_and_nonleaf_nodes_are_not_flattened(self):
        for kind in ('document', 'heading', 'list', 'item', 'paragraph'):
            node = NodeStub(1, kind, r'\textbf{Do not flatten}',
                            children=[NodeStub(2, 'sentence', 'child')])
            before = deepcopy(node)
            result = normalize_parser_leaf(node, after_a3=True, mode='prose')
            self.assertEqual(result.content, node.raw)
            self.assertFalse(result.applied)
            self.assertEqual(node, before)
        heading = NodeStub(1, 'heading', r'\paragraph{Heading}',
                           attrs={'level': 5, 'title': 'Heading', 'appendix': True})
        self.assertFalse(normalize_parser_leaf(heading, after_a3=True, mode='prose').applied)

    def test_default_preserve_does_not_assume_a3_kind_vocabulary(self):
        node = NodeStub(9, 'future_kind', r'\textbf{Content}')
        result = normalize_parser_leaf(node, after_a3=True)
        self.assertEqual(result.content, node.raw)
        self.assertFalse(result.applied)

    def test_figure_table_and_code_are_deferred(self):
        for kind in ('figure', 'table', 'verbatim', 'code'):
            node = NodeStub(1, kind, r'\textbf{Protected}\cite{foo}')
            result = normalize_parser_leaf(node, after_a3=True, mode='prose')
            self.assertEqual(result.content, node.raw)
            self.assertFalse(result.applied)
            self.assertEqual(result.diagnostics[0].code, 'parser_region_policy_deferred')

    def test_explicit_math_modes_and_complex_math_preservation(self):
        inline = NodeStub(1, 'inline_math', r'\(x+y\)')
        self.assertEqual(normalize_parser_leaf(inline, after_a3=True, mode='inline_math').content, '$x+y$')
        block = NodeStub(2, 'block_math', r'\[x+y\]')
        self.assertEqual(normalize_parser_leaf(block, after_a3=True, mode='block_math').content,
                         r'\begin{equation*}x+y\end{equation*}')
        complex_node = NodeStub(3, 'block_math', r'\begin{align}x&=y\label{x}\end{align}')
        result = normalize_parser_leaf(complex_node, after_a3=True, mode='block_math')
        self.assertEqual(result.content, complex_node.raw)
        self.assertFalse(result.diagnostics)

    def test_local_warning_spans_do_not_pretend_to_be_file_spans(self):
        node = NodeStub(1, 'sentence', 'Nguyễn: $unclosed',
                        [{'opaque': 'file_a'}, {'opaque': 'file_b'}])
        result = normalize_parser_leaf(node, after_a3=True, mode='prose')
        warning = result.diagnostics[0]
        self.assertEqual(warning.offset_basis, 'node_raw')
        self.assertEqual(warning.source_mapping, 'container_only')
        self.assertEqual(warning.severity, 'warning')
        self.assertEqual(node.raw[warning.start:warning.end], '$unclosed')
        self.assertEqual(warning.source_spans, tuple(node.source_spans))

    def test_occurrences_and_versions_are_not_deduplicated(self):
        first = normalize_parser_leaf(NodeStub(1, 'sentence', 'Same.'), after_a3=True, mode='prose')
        second = normalize_parser_leaf(NodeStub(2, 'sentence', 'Same.'), after_a3=True, mode='prose')
        self.assertEqual(first.content, second.content)
        self.assertNotEqual(first.occurrence_id, second.occurrence_id)

    def test_bibliography_block_with_moved_heading_and_opaque_multifile_spans(self):
        raw = (r'\section*{References}' + '\n' + r'\begin{thebibliography}{1}'
               r'\bibitem{k}Body.\end{thebibliography}')
        block = BlockStub(raw, [{'opaque': 'heading.tex'}, {'opaque': 'refs.tex'}])
        before = deepcopy(block)
        result = extract_parser_bibliography(block, arxiv_id='2411.18799', version=2)
        entry = result.bibliography.entries[0]
        self.assertEqual(entry.citation_key, 'k')
        self.assertEqual(entry.source.arxiv_id, '2411.18799')
        self.assertEqual(entry.source.version, 2)
        self.assertIsNone(entry.source.source_file)
        self.assertEqual(raw[entry.start:entry.end], entry.raw)
        self.assertEqual(result.source_spans, tuple(block.source_spans))
        self.assertEqual(block, before)

    def test_malformed_block_and_external_bibliography_keep_diagnostics(self):
        for raw in (r'\begin{thebibliography}{1}\bibitem{k}Body.', r'\bibliography{refs}'):
            result = extract_parser_bibliography(BlockStub(raw, []), arxiv_id='x', version=1)
            self.assertEqual(result.raw, raw)
            self.assertTrue(result.diagnostics)
            self.assertEqual(result.bibliography.exclusion_spans, ())
            self.assertEqual(result.diagnostics[0].offset_basis, 'bibliography_block_raw')


    def test_invalid_adapter_inputs_are_rejected(self):
        node = NodeStub(1, 'sentence', 'text')
        for kwargs, error in (({'after_a3': 'yes'}, TypeError),
                              ({'after_a3': True, 'mode': 'auto'}, ValueError)):
            with self.assertRaises(error):
                normalize_parser_leaf(node, **kwargs)
        for version in (None, 0, True):
            with self.assertRaises(ValueError):
                extract_parser_bibliography(BlockStub('', []), arxiv_id='x', version=version)
        with self.assertRaises(TypeError):
            extract_parser_bibliography(object(), arxiv_id='x', version=1)


if __name__ == '__main__':
    unittest.main()
