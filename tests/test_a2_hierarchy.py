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
