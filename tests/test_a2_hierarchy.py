from parser.gather import gather_version
from parser.hierarchy import build_hierarchy


def _tree(tmp_path, main, extras=None):
    (tmp_path / 'main.tex').write_text(main, encoding='utf8')
    for name, body in (extras or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding='utf8')
    return build_hierarchy(gather_version('2504.02523', 2, tmp_path))


def test_headings_skip_levels_without_synthetic_nodes_and_keep_appendix(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
Intro text.\section{Intro}First.\subsubsection{Deep}Deep text.
\section*{Acknowledgements}Thanks.\appendix\section{Proof}Proof text.
\end{document}''')
    root = result.root
    assert result.status == 'ready'
    assert [n.kind for n in root.children] == ['paragraph', 'heading', 'heading', 'heading']
    intro, acknowledgements, appendix = root.children[1:]
    assert intro.attrs == {'level': 2, 'starred': False, 'appendix': False, 'title': 'Intro'}
    assert [n.kind for n in intro.children] == ['paragraph', 'heading']
    assert intro.children[1].attrs['level'] == 4
    assert acknowledgements.attrs['starred'] is True
    assert appendix.attrs['appendix'] is True
    assert len({n.occurrence_id for n in [root, *root.children, *intro.children]}) == 1 + len(root.children) + len(intro.children)


def test_nested_lists_have_item_parents_and_preserve_order(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\section{Steps}\begin{enumerate}\item First
\begin{itemize}\item Inner\end{itemize}\item Second\end{enumerate}
\end{document}''')
    heading = result.root.children[0]
    outer = heading.children[0]
    assert outer.kind == 'list' and outer.attrs['environment'] == 'enumerate'
    assert [n.kind for n in outer.children] == ['item', 'item']
    assert [n.kind for n in outer.children[0].children] == ['paragraph', 'list']
    assert outer.children[0].children[1].children[0].children[0].raw.strip() == 'Inner'
    assert outer.children[1].children[0].raw.strip() == 'Second'


def test_bibliography_is_separate_but_uncertain_references_heading_is_kept(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\section{Body}Text.\section*{References}
\begin{thebibliography}{9}\bibitem{x} One.\end{thebibliography}
\section{After}More.\end{document}''')
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['Body', 'After']
    assert len(result.bibliography) == 1
    assert r'\section*{References}' in result.bibliography[0].raw
    assert r'\bibitem{x}' in result.bibliography[0].raw
    uncertain = _tree(tmp_path, r'\documentclass{article}\begin{document}\section{References}Discussion.\end{document}')
    assert uncertain.status == 'partial'
    assert uncertain.root.children[0].attrs['title'] == 'References'
    assert 'uncertain_references' in [d.code for d in uncertain.diagnostics]


def test_paragraph_spans_follow_cross_file_text_and_ignore_fake_headings(tmp_path):
    result = _tree(tmp_path,
        r'\documentclass{article}\begin{document}\section{One}Hello \input{part} tail.\end{document}',
        {'part.tex': r'from \textbf{another} file.'})
    paragraph = result.root.children[0].children[0]
    assert [s.path for s in paragraph.source_spans] == ['main.tex', 'part.tex', 'main.tex']
    assert 'from' in paragraph.raw and 'tail.' in paragraph.raw
    assert all(span.start < span.end for span in paragraph.source_spans)


def test_unparseable_a1_does_not_create_tree(tmp_path):
    result = _tree(tmp_path, 'No document class')
    assert result.status == 'unparseable'
    assert result.root is None


def test_blank_line_splits_paragraphs_and_macro_body_does_not_create_heading(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\newcommand{\fake}{\section{Wrong}}
First paragraph.

Second paragraph.\end{document}''')
    assert [n.kind for n in result.root.children] == ['paragraph', 'paragraph']
    assert 'First paragraph.' in result.root.children[0].raw
    assert result.root.children[1].raw == 'Second paragraph.'


def test_heading_inside_second_command_argument_is_not_structural(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\href{https://example.org}{\section{Fake}}\section{Real}Text.\end{document}''')
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['Real']


def test_opaque_blocks_with_blank_lines_reach_a3_as_one_paragraph(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}\section{Method}
\begin{equation}a=b

c=d\end{equation}
\begin{figure}\caption{A}\begin{tabular}{c}x\end{tabular}

\caption{B}\end{figure}
\begin{verbatim}\section{Fake}

literal\end{verbatim}\end{document}''')
    children = result.root.children[0].children
    assert result.status == 'ready'
    assert len(children) == 1
    assert all(marker in children[0].raw for marker in
               (r'\end{equation}', r'\end{figure}', r'\end{verbatim}'))


def test_unclosed_opaque_block_is_partial_with_raw_preserved(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{align}a&=b

c&=d''')
    assert result.status == 'partial'
    assert 'unclosed_protected' in [d.code for d in result.diagnostics]
    assert len(result.root.children) == 1
    assert r'\begin{align}' in result.root.children[0].raw


def test_bibliography_command_is_separated_only_with_clear_boundary(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\section{Body}Body.\section{References}\bibliography{refs}
\section{After}After.\end{document}''')
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['Body', 'After']
    assert len(result.bibliography) == 1
    assert r'\section{References}' in result.bibliography[0].raw
    assert r'\bibliography{refs}' in result.bibliography[0].raw
    assert result.bibliography[0].source_spans[0].path == 'main.tex'


def test_math_delimiters_with_blank_lines_remain_whole(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
Start $x+

y$ and \[a

b\] then $$c

d$$ end.\end{document}''')
    assert result.status == 'ready'
    assert len(result.root.children) == 1
    assert result.root.children[0].raw.endswith('end.')


def test_protected_block_keeps_cross_file_provenance(tmp_path):
    result = _tree(tmp_path,
        r'\documentclass{article}\begin{document}\begin{equation}a\input{part}z\end{equation}\end{document}',
        {'part.tex': 'b\n\nc'})
    assert len(result.root.children) == 1
    paragraph = result.root.children[0]
    assert [span.path for span in paragraph.source_spans] == ['main.tex', 'part.tex', 'main.tex']
    assert 'b\n\nc' in paragraph.raw


def test_unclosed_bibliography_is_partial_and_keeps_raw(tmp_path):
    result = _tree(tmp_path, r'\documentclass{article}\begin{document}\begin{thebibliography}{9}\bibitem{x} Text')
    assert result.status == 'partial'
    assert 'unclosed_protected' in [d.code for d in result.diagnostics]
    assert len(result.bibliography) == 0
    assert r'\bibitem{x}' in result.root.children[0].raw


def test_algorithm_block_keeps_caption_blank_lines_and_fake_heading(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}\section{Method}
\begin{algorithm}\caption{Training}

\begin{algorithmic}\STATE Start\end{algorithmic}
\section{Fake}\end{algorithm}\section{Real}Text.\end{document}''')
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['Method', 'Real']
    block = result.root.children[0].children
    assert len(block) == 1 and r'\end{algorithm}' in block[0].raw


def test_protected_end_in_comment_or_verb_is_not_a_real_terminator(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{figure}
% \end{figure}
\verb|\end{figure}|

\caption{Actual}\end{figure}\section{After}Text.\end{document}''')
    assert result.status == 'ready'
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['After']
    assert len(result.root.children[0:1]) == 1
    assert r'\caption{Actual}' in result.root.children[0].raw


def test_bibliography_end_in_comment_does_not_truncate_block(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\section{References}\begin{thebibliography}{9}
% \end{thebibliography}
\bibitem{x} Real.\end{thebibliography}\section{After}More.\end{document}''')
    assert result.status == 'ready'
    assert len(result.bibliography) == 1
    assert r'\bibitem{x} Real.' in result.bibliography[0].raw
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['After']


def test_references_intro_and_printbibliography_move_to_bib_block(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\section{Body}Text.\section{References}
Introductory references note.

\printbibliography\section{After}More.\end{document}''')
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['Body', 'After']
    assert len(result.bibliography) == 1
    assert 'Introductory references note.' in result.bibliography[0].raw
    assert r'\printbibliography' in result.bibliography[0].raw


def test_a2_keeps_raw_immutable_and_leaves_content_for_b1(tmp_path):
    result = _tree(tmp_path, r'\documentclass{article}\begin{document}\section{Intro}Raw text.\end{document}')
    heading = result.root.children[0]
    assert heading.raw == r'\section{Intro}'
    assert heading.content is None
    assert heading.children[0].raw == 'Raw text.'
    assert heading.children[0].content is None


def test_literal_verbatim_does_not_nest_on_printed_begin_command(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{verbatim}\begin{verbatim} printed text\end{verbatim}\section{After}Done.\end{document}''')
    assert result.status == 'ready'
    assert [n.attrs['title'] for n in result.root.children if n.kind == 'heading'] == ['After']


def test_protected_end_inside_macro_body_is_ignored(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{figure}\newcommand{\fake}{\end{figure}}

\section{Fake}\caption{Actual result}\end{figure}
\section{Real}Content.\end{document}''')
    assert result.status == 'ready'
    assert [node.attrs['title'] for node in result.root.children
            if node.kind == 'heading'] == ['Real']
    block = result.root.children[0]
    assert block.kind == 'paragraph'
    assert r'\newcommand{\fake}{\end{figure}}' in block.raw
    assert r'\caption{Actual result}' in block.raw


def test_protected_end_inside_ordinary_command_argument_is_ignored(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{figure}\href{url}{literal \end{figure} text}

\caption{Actual}\end{figure}\section{Real}Content.\end{document}''')
    assert result.status == 'ready'
    assert [node.attrs['title'] for node in result.root.children
            if node.kind == 'heading'] == ['Real']
    assert len([node for node in result.root.children if node.kind == 'paragraph']) == 1
    assert r'\caption{Actual}' in result.root.children[0].raw


def test_protected_end_inside_unbraced_macro_definition_is_ignored(tmp_path):
    result = _tree(tmp_path, r'''\documentclass{article}\begin{document}
\begin{figure}\DeclareRobustCommand\fake[1]{\end{figure}}

\caption{Actual}\end{figure}\section{Real}Text.\end{document}''')
    assert result.status == 'ready'
    assert [node.attrs['title'] for node in result.root.children
            if node.kind == 'heading'] == ['Real']
    assert len([node for node in result.root.children if node.kind == 'paragraph']) == 1
    assert r'\caption{Actual}' in result.root.children[0].raw
