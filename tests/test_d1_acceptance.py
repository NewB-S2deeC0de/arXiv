"""Cross-stage offline acceptance; no fake service is a real-parser pass."""
from dataclasses import asdict
import socket
import json
import random

import pyarrow.parquet as pq

import pytest

from parser import gather_version, build_hierarchy, refine_hierarchy
from parser.integration import load_services
from parser.pipeline import run_parser


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('D1 must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def test_d1_real_a_stages_are_deterministic_and_read_only(tmp_path):
    files = {
        'paper.tex': r'\documentclass{article}\begin{document}\input{intro}\end{document}',
        'intro.tex': r'\section*{Introduction}First sentence. Second sentence.',
        'unused.tex': 'UNUSED TEXT',
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding='utf8')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    def parse():
        gathered = gather_version('2504.02523', 1, tmp_path)
        return refine_hierarchy(build_hierarchy(gathered))

    first, second = parse(), parse()
    assert first.status == 'ready'
    assert asdict(first) == asdict(second)
    sentences = [n for n in walk(first.root) if n.kind == 'sentence']
    assert [n.raw.strip() for n in sentences] == ['First sentence.', 'Second sentence.']
    for node in sentences:
        reconstructed = ''.join(files[s.path][s.start:s.end] for s in node.source_spans)
        assert reconstructed == node.raw
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.requires_bc_integration
def test_d1_real_pipeline_can_finalize_content(tmp_path, make_source):
    load_services()
    root, output = tmp_path / 'runtime', tmp_path / 'parsed'
    for version in (1, 2):
        make_source(root, '2504.02523', version, {
            'paper.tex': r"""\documentclass{article}\begin{document}
\section{Result}Use $x$ with \cite{same}.
\bibliography{selected}\end{document}""",
            'selected.bib': ('@article{same, title={Reference %d}, '
                             'doi={10.1234/ref%d}}' % (version, version)),
            'unused.bib': '@article{unused, title={Must not be exported}, doi={10.1234/unused}}',
        })
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    manifest = run_parser(root, output_root=output, resume=False)
    assert manifest['versions']['versions_parsed'] == 2
    assert manifest['versions']['versions_pending'] == 0
    assert manifest['validation']['ok']
    entries = pq.read_table(output / 'bibtex.parquet').to_pylist()
    keys = {row['doi']: row['citation_key'] for row in entries}
    assert set(keys) == {'10.1234/ref1', '10.1234/ref2'}
    assert keys['10.1234/ref1'] != keys['10.1234/ref2']
    elements = {row['element_id']: row for row in
                pq.read_table(output / 'elements.parquet').to_pylist()}
    edges = pq.read_table(output / 'hierarchy.parquet').to_pylist()
    for version in (1, 2):
        contents = [elements[e['element_id']]['content'] for e in edges if e['version'] == version]
        assert any(r'\cite{' + keys[f'10.1234/ref{version}'] + '}' in text for text in contents)
        assert not any(r'\cite{same}' in text for text in contents)
    assert isinstance(json.loads((output / 'bibtex_audit.json').read_text(encoding='utf8')), dict)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_d1_published_hierarchy_can_preserve_source_order():
    from parser.contract import HIERARCHY_SCHEMA
    import pyarrow as pa
    assert 'position' in HIERARCHY_SCHEMA.names
    assert HIERARCHY_SCHEMA.field('position').type == pa.int64()


def test_d1_acknowledgments_remain_in_body(tmp_path):
    (tmp_path / 'paper.tex').write_text(r"""\documentclass{article}\begin{document}
\section*{Acknowledgements}Thanks to our collaborators.
\begin{thebibliography}{1}\bibitem{x} A reference.\end{thebibliography}
\end{document}""", encoding='utf8')
    result = refine_hierarchy(build_hierarchy(gather_version('2504.02523', 1, tmp_path)))
    assert result.status == 'ready'
    heading = next(n for n in walk(result.root) if n.attrs.get('title') == 'Acknowledgements')
    assert [n.raw.strip() for n in walk(heading) if n.kind == 'sentence'] == [
        'Thanks to our collaborators.']
    assert len(result.bibliography) == 1


def test_d1_source_order_roundtrip_and_old_checkpoint_invalidation(tmp_path, make_source):
    root, output = tmp_path / 'runtime', tmp_path / 'parsed'
    bodies = {
        1: r'\section{First}Alpha. Beta. Alpha.\section{Second}Nested.',
        2: r'\section{Second}Nested.\section{First}Beta. Alpha. Beta.',
    }
    for version, body in bodies.items():
        make_source(root, '2504.02523', version, {
            'paper.tex': r'\documentclass{article}\begin{document}' + body + r'\end{document}'})
    run_parser(root, output_root=output, resume=False)
    elements = {r['element_id']: r for r in pq.read_table(output / 'elements.parquet').to_pylist()}
    edges = pq.read_table(output / 'hierarchy.parquet').to_pylist()
    random.Random(7).shuffle(edges)
    audit = json.loads((output / 'bibtex_audit.json').read_text(encoding='utf8'))

    def rich_tree(node):
        return node['kind'], node['content'], tuple(rich_tree(c) for c in node['children'])

    for version in (1, 2):
        by_parent = {}
        version_edges = [r for r in edges if r['version'] == version]
        for row in version_edges:
            by_parent.setdefault(row['parent_id'], []).append(row)
        root_ids = {r['parent_id'] for r in version_edges
                    if elements[r['parent_id']]['kind'] == 'document'}
        assert len(root_ids) == 1

        def rebuild(element_id):
            element = elements[element_id]
            children = sorted(by_parent.get(element_id, []), key=lambda r: r['position'])
            assert [r['position'] for r in children] == list(range(len(children)))
            return element['kind'], element['content'], tuple(rebuild(r['element_id']) for r in children)

        rebuilt = rebuild(root_ids.pop())
        reference = next(v for v in audit['publications']['2504.02523']['versions']
                         if v['version'] == version)['finalized']['tree']
        assert rebuilt == rich_tree(reference)
        assert [n[1] for n in rebuilt[2]] == (['First', 'Second'] if version == 1 else ['Second', 'First'])
        first = next(n for n in rebuilt[2] if n[1] == 'First')
        sentences = [n[1] for paragraph in first[2] for n in paragraph[2] if n[0] == 'sentence']
        assert sentences == (['Alpha.', 'Beta.', 'Alpha.'] if version == 1
                             else ['Beta.', 'Alpha.', 'Beta.'])

    checkpoint = json.loads((output / 'checkpoint.json').read_text(encoding='utf8'))
    checkpoint['schema_version'] = 2
    (output / 'checkpoint.json').write_text(json.dumps(checkpoint), encoding='utf8')
    shard = output / '_parts' / '2504.02523' / 'hierarchy.parquet'
    pq.write_table(pq.read_table(shard).drop(['position']), shard)
    log = []
    run_parser(root, output_root=output, resume=True, log=log.append)
    assert not any('skip 2504.02523' in line for line in log)
    assert 'position' in pq.read_schema(shard).names
    assert pq.read_table(output / 'hierarchy.parquet').num_rows == len(edges)
