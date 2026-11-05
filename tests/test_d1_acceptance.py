"""Cross-stage offline acceptance; no fake service is a real-parser pass."""
from dataclasses import asdict
import socket

import pytest

from parser import gather_version, build_hierarchy, refine_hierarchy
from parser.integration import load_services


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


@pytest.mark.xfail(strict=True, raises=(ImportError, RuntimeError),
                   reason='B1/B2/B3 real services are not integrated; do not count fake C3 tests as acceptance')
def test_d1_real_pipeline_can_finalize_content(tmp_path):
    (tmp_path / 'paper.tex').write_text(
        r'\documentclass{article}\begin{document}A sentence.\end{document}', encoding='utf8')
    services = load_services()
    gathered = services.gather('2504.02523', 1, tmp_path)
    parsed = services.parse_document(gathered, '2504.02523', 1)
    assert parsed.tree is not None, 'B1 must finalize the tree before C2'


@pytest.mark.xfail(strict=True, reason='C1/C2 hierarchy schema has no source-order position yet')
def test_d1_published_hierarchy_can_preserve_source_order():
    from parser.contract import HIERARCHY_SCHEMA
    assert 'position' in HIERARCHY_SCHEMA.names
