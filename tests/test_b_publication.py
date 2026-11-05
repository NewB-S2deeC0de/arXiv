from types import SimpleNamespace

import pytest

from standardization import MappedSourceSpan, PublicationVersionInput, standardize_publication
from standardization import normalize_block_math, normalize_inline_math
from test_b1_b2_b3_version_workflow import FrozenHierarchy, FrozenNode


def make_version(root, version, key, fields, declaration=r'\bibliography{refs}'):
    root.mkdir()
    (root / 'refs.bib').write_text(f'@article{{{key}, {fields}}}', encoding='utf-8')
    (root / 'unused.bib').write_text('@misc{unused, title={Ignore}}', encoding='utf-8')
    leaf = FrozenNode(3, 'sentence', rf'The result is \(x\) \cite{{{key}}}.')
    heading = FrozenNode(2, 'heading', r'\section{Result}', children=(leaf,),
                         attrs={'title': r'\textbf{Result}'})
    hierarchy = FrozenHierarchy(version=version,
        root=FrozenNode(1, 'document', '', children=(heading,)))
    segment = SimpleNamespace(text=declaration,
        origin=MappedSourceSpan('main.tex', 0, len(declaration)))
    gathered = SimpleNamespace(arxiv_id='p', version=version, status='ready',
        root_file='main.tex', segments=(segment,), bib_files=('refs.bib', 'unused.bib'), diagnostics=())
    return PublicationVersionInput(hierarchy, gathered, root)


def test_publication_from_static_bib_selection_to_reviewed_cross_version_remap(tmp_path):
    metadata = 'title={Result}, author={Doe, Jane}'
    inputs = [make_version(tmp_path / 'v1', 1, 'old', metadata + ', year={2020}'),
              make_version(tmp_path / 'v2', 2, 'new', metadata + ', year={2020}, doi={10.1234/result}'),
              make_version(tmp_path / 'v3', 3, 'latest', metadata + ', doi={10.1234/result}')]
    pending = standardize_publication('p', inputs, node_factory=SimpleNamespace)
    assert len(pending.references.merge.records) == 2
    candidate, = pending.references.metadata_candidates
    approved = standardize_publication('p', reversed(inputs), node_factory=SimpleNamespace,
        approved_metadata_candidates=[candidate.candidate_id])
    record, = approved.references.merge.records
    assert record.payload.year == 2020
    assert record.payload.doi == '10.1234/result'
    assert len(record.members) == 3
    for version in approved.versions:
        assert [r.path for r in version.bibliography.resources] == ['refs.bib']
        heading, = version.finalized.tree.children
        assert heading.content == 'Result'
        assert heading.children[0].content == rf'The result is $x$ \cite{{{record.payload.citation_key}}}.'
    with pytest.raises(ValueError, match='unknown metadata candidate'):
        standardize_publication('p', inputs, node_factory=SimpleNamespace,
                                approved_metadata_candidates=['stale-review'])


def test_missing_bib_marks_version_partial_with_audit(tmp_path):
    version = make_version(tmp_path / 'v1', 1, 'old', 'title={Result}', r'\bibliography{missing}')
    result = standardize_publication('p', [version], node_factory=SimpleNamespace)
    staged, = result.versions
    assert staged.status == 'partial'
    assert staged.finalized is None
    assert staged.bibliography.selection_diagnostics[0].code == 'bibliography_dependency_not_in_listed_bib_files'


def test_math_canonical_forms_preserve_display_numbering_and_multiline_structure():
    assert normalize_inline_math(r'\begin{math}x\end{math}').content == '$x$'
    unnumbered = r'\begin{equation*}x\end{equation*}'
    for raw in (r'\[x\]', '$$x$$', r'\begin{displaymath}x\end{displaymath}'):
        assert normalize_block_math(raw).content == unnumbered
    aligned = r'\begin{align}x&=1\notag\\y&=2\label{y}\end{align}'
    assert normalize_block_math(aligned).content == aligned
