from parser.gather import gather_version
from parser.hierarchy import build_hierarchy
from parser.leaves import refine_hierarchy


def _parse(tmp_path, body):
    (tmp_path / 'main.tex').write_text(
        r'\documentclass{article}\begin{document}' + body + r'\end{document}',
        encoding='utf8')
    source = build_hierarchy(gather_version('2504.02523', 2, tmp_path))
    return source, refine_hierarchy(source)


def _leaves(node):
    return [node] if not node.children else [leaf for child in node.children for leaf in _leaves(child)]


def test_sentence_boundaries_and_source_spans(tmp_path):
    original, result = _parse(tmp_path, r'One sentence. Next sentence! Last?')
    paragraph = result.root.children[0]
    assert paragraph.kind == 'paragraph'
    assert [c.raw for c in paragraph.children] == ['One sentence.', 'Next sentence!', 'Last?']
    assert all(c.kind == 'sentence' for c in paragraph.children)
    assert original.root.children[0].children == []
    assert all(c.content is None for c in paragraph.children)
    assert all(sum(s.end - s.start for s in c.source_spans) == len(c.raw)
               for c in paragraph.children)
    assert refine_hierarchy(original) == result


def test_protected_inline_content_and_abbreviations(tmp_path):
    _, result = _parse(tmp_path, (
        r'Fig. 1 shows i.e. a result by et al. at 3.14. '
        r'See https://example.org/a.b and \cite{a.b} with $x.y$ or \(a.b\). Done.'))
    sentences = result.root.children[0].children
    assert len(sentences) == 3
    assert sentences[0].raw.endswith('3.14.')
    assert sentences[1].raw.endswith(r'\(a.b\).')
    assert sentences[2].raw == 'Done.'


def test_block_formula_and_figure_order_and_caption(tmp_path):
    _, result = _parse(tmp_path, (
        r'Before. \begin{align}a&=b\label{eq:a}\end{align} After. '
        r'$$c=d$$ \[e=f\] '
        r'\begin{table}\caption{Values. More values.}\label{t:1}'
        r'\begin{tabular}{c}x\end{tabular}\end{table} End.'))
    nodes = result.root.children
    assert [n.kind for n in nodes] == [
        'paragraph', 'block_formula', 'paragraph', 'block_formula',
        'block_formula', 'figure', 'paragraph']
    assert nodes[1].raw.endswith(r'\end{align}')
    assert nodes[1].attrs['environment'] == 'align'
    assert nodes[5].attrs['subtype'] == 'table'
    assert nodes[5].attrs['caption'] == 'Values. More values.'
    assert nodes[5].attrs['label'] == 't:1'
    assert nodes[5].raw.count('Values. More values.') == 1
    assert [n.raw for n in _leaves(result.root) if n.kind == 'sentence'] == [
        'Before.', 'After.', 'End.']


def test_nested_list_and_appendix_keep_parentage(tmp_path):
    _, result = _parse(tmp_path, (
        r'\section{Main}\begin{enumerate}\item First. Second.'
        r'\begin{itemize}\item Inner.\end{itemize}\end{enumerate}'
        r'\appendix\section{Proof}Finally.'))
    heading, appendix = result.root.children
    item = heading.children[0].children[0]
    assert [c.raw for c in item.children[0].children] == ['First.', 'Second.']
    assert item.children[1].children[0].children[0].children[0].raw == 'Inner.'
    assert appendix.attrs['appendix'] is True
    assert appendix.children[0].children[0].raw == 'Finally.'


def test_unsupported_literal_is_partial_and_preserved(tmp_path):
    _, result = _parse(tmp_path, r'Before. \begin{verbatim}A. B.\end{verbatim} After.')
    assert result.status == 'partial'
    assert any(d.code == 'unsupported_literal' for d in result.diagnostics)
    assert [n.kind for n in result.root.children] == ['paragraph']
    assert result.root.children[0].raw.endswith('After.')


def test_block_spans_across_input_files_and_fake_delimiter_in_command(tmp_path):
    (tmp_path / 'main.tex').write_text(
        r'\documentclass{article}\begin{document}A. '
        r'\begin{figure}\caption{A}\input{part}\end{figure} B.\end{document}',
        encoding='utf8')
    (tmp_path / 'part.tex').write_text(
        r'\somecommand*{\end{figure}}\label{real}', encoding='utf8')
    source = build_hierarchy(gather_version('2504.02523', 2, tmp_path))
    result = refine_hierarchy(source)
    figure = result.root.children[1]
    assert [n.kind for n in result.root.children] == ['paragraph', 'figure', 'paragraph']
    assert [s.path for s in figure.source_spans] == ['main.tex', 'part.tex', 'main.tex']
    assert figure.attrs['label'] == 'real'
    assert sum(s.end - s.start for s in figure.source_spans) == len(figure.raw)
