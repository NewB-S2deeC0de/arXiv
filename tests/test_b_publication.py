from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from standardization import MappedSourceSpan, PublicationVersionInput, b3_audit_payload, standardize_publication
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


@pytest.mark.parametrize('include_partial', [False, True])
def test_invalid_source_mapping_marks_ready_version_partial(tmp_path, include_partial):
    item = make_version(tmp_path / 'v1', 1, 'old', 'title={Result}')
    heading, = item.hierarchy.root.children
    leaf, = heading.children
    leaf = replace(leaf, source_spans=(MappedSourceSpan('main.tex', 0, len(leaf.raw)),))
    root = replace(item.hierarchy.root, children=(replace(heading, children=(leaf,)),))
    item = replace(item, hierarchy=replace(item.hierarchy, root=root),
                   source_texts={'main.tex': '!' * len(leaf.raw)})

    result = standardize_publication('p', [item], node_factory=SimpleNamespace,
                                    include_partial=include_partial)
    version, = result.versions
    assert item.hierarchy.status == 'ready'
    assert version.bibliography.selection_diagnostics == ()
    assert version.status == 'partial'
    assert version.normalized.source_mapping_failures == 1
    normalized, = version.normalized.nodes
    assert normalized.raw == normalized.content == leaf.raw
    assert normalized.source_spans == leaf.source_spans
    assert any(d.code == 'normalization_source_mapping_unavailable'
               for d in version.normalized.diagnostics)
    assert (version.finalized is not None) == include_partial
    assert len(result.references.merge.records) == int(include_partial)


def test_math_canonical_forms_preserve_display_numbering_and_multiline_structure():
    assert normalize_inline_math(r'\begin{math}x\end{math}').content == '$x$'
    unnumbered = r'\begin{equation*}x\end{equation*}'
    for raw in (r'\[x\]', '$$x$$', r'\begin{displaymath}x\end{displaymath}'):
        assert normalize_block_math(raw).content == unnumbered
    aligned = r'\begin{align}x&=1\notag\\y&=2\label{y}\end{align}'
    assert normalize_block_math(aligned).content == aligned


@pytest.fixture
def parsed_publication_versions(tmp_path):
    gather = pytest.importorskip('parser.gather')
    hierarchy = pytest.importorskip('parser.hierarchy')
    leaves = pytest.importorskip('parser.leaves')
    inputs = []
    for version in (1, 2):
        source = tmp_path / f'v{version}'
        source.mkdir()
        tex = (r'\documentclass{article}\begin{document}'
               r'This result follows \cite{same,inline}.'
               r'\bibliography{refs}'
               r'\begin{thebibliography}{1}\bibitem{inline}An inline reference.'
               r'\end{thebibliography}\end{document}')
        (source / 'main.tex').write_text(tex, encoding='utf-8')
        (source / 'refs.bib').write_text(
            f'@article{{same, title={{Version {version}}}, doi={{10.1234/ref{version}}}}}',
            encoding='utf-8')
        (source / 'unused.bib').write_text('@misc{unused, title={Unused}}', encoding='utf-8')
        gathered = gather.gather_version('2411.18799', version, source)
        a3 = leaves.refine_hierarchy(hierarchy.build_hierarchy(gathered))
        inputs.append(PublicationVersionInput(a3, gathered, source, {'main.tex': tex}))

    return inputs


def test_actual_a_parser_handoff_remaps_same_key_per_version(parsed_publication_versions):
    arxiv_id = '2411.18799'
    result = standardize_publication(arxiv_id, parsed_publication_versions, node_factory=SimpleNamespace)
    mapping = result.references.merge.mapping
    assert mapping[arxiv_id, 1, 'same'] != mapping[arxiv_id, 2, 'same']
    assert len(result.references.merge.records) == 4
    for version in result.versions:
        assert version.status == 'ready'
        assert version.normalized.source_mapping_failures == 0
        assert [resource.path for resource in version.bibliography.resources] == ['refs.bib']
        keys = {o.prepared.entry.citation_key for o in version.bibliography.occurrences}
        assert keys == {'same', 'inline'}
        assert all(o.source_spans for o in version.bibliography.occurrences)
        same = mapping[arxiv_id, version.version, 'same']
        inline = mapping[arxiv_id, version.version, 'inline']
        paragraph, = version.finalized.tree.children
        sentence, = paragraph.children
        assert sentence.kind == 'sentence'
        assert sentence.content == rf'This result follows \cite{{{same},{inline}}}.'


def test_audit_retains_ambiguous_key_after_explicit_bib_override(tmp_path):
    item = make_version(tmp_path / 'v1', 1, 'old', 'title={Result}, doi={10.1234/one}')
    (item.source_dir / 'unused.bib').write_text(
        '@article{old, title={Other}, doi={10.1234/two}}', encoding='utf-8')
    item = replace(item, selected_bib_files=('refs.bib', 'unused.bib'))
    result = standardize_publication('p', [item], node_factory=SimpleNamespace)

    version, = result.versions
    assert version.status == 'ready'
    heading, = version.finalized.tree.children
    assert heading.children[0].content == r'The result is $x$ \cite{old}.'
    assert len(result.references.merge.records) == 2
    audit = json.loads(json.dumps(b3_audit_payload(result.references)))
    assert audit['result']['merge']['mapping'] == [
        {'arxiv_id': 'p', 'version': 1, 'old_key': 'old', 'canonical_key': None},
    ]


def test_actual_c2_accepts_finalized_tree_and_bibtex_rows(parsed_publication_versions, tmp_path):
    contract = pytest.importorskip('parser.contract')
    dedup = pytest.importorskip('parser.dedup')
    validation = pytest.importorskip('parser.validation')
    pa = pytest.importorskip('pyarrow')
    parquet = pytest.importorskip('pyarrow.parquet')
    arxiv_id = '2411.18799'
    result = standardize_publication(arxiv_id, parsed_publication_versions,
                                     node_factory=contract.TreeNode)
    store = dedup.ElementStore(arxiv_id)
    for version in result.versions:
        store.add_tree(version.version, version.finalized.tree)
    schema = contract.BIBTEX_SCHEMA
    bibtex = []
    for record in result.references.merge.records:
        payload = record.payload.as_kwargs()
        bibtex.append({name: payload.get(name) for name in schema.names})
    rows = {
        'elements': contract.element_rows(store.sorted_elements()),
        'hierarchy': contract.hierarchy_rows(store.sorted_edges()),
        'bibtex': bibtex,
    }
    validation.validate_or_raise(arxiv_id, rows)
    assert len(bibtex) == 4
    path = tmp_path / 'bibtex.parquet'
    parquet.write_table(pa.Table.from_pylist(bibtex, schema=schema), path)
    assert parquet.read_table(path).to_pylist() == bibtex
