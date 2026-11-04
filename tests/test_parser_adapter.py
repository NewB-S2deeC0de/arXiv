from parser.adapter import iter_source_records, publications_with_records
from parser.contract import SourceStatus

TEX = r'\documentclass{article}\begin{document}Hello world.\end{document}'


def test_adapter_reads_success_empty_and_missing(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': TEX}, 'succeeded')
    make_source(root, '2410.23025', 2, {}, 'no_tex')
    (root / 'data' / '2410.23025' / 'versions' / 'v3').mkdir(parents=True)
    records = list(iter_source_records(root))
    assert [r.version for r in records] == [1, 2, 3]
    assert records[0].status is SourceStatus.SUCCEEDED and records[0].has_tex
    assert records[1].status is SourceStatus.NO_TEX and not records[1].has_tex
    assert records[2].status is SourceStatus.MISSING


def test_adapter_corrupt_inventory_is_failed(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23026', 1, {'main.tex': TEX}, corrupt_inventory=True)
    record = list(iter_source_records(root))[0]
    assert record.status is SourceStatus.FAILED
    assert record.error == 'inventory_checksum_mismatch'


def test_adapter_limit_counts_publications_not_versions(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': TEX})
    make_source(root, '2410.23025', 2, {'main.tex': TEX})
    make_source(root, '2410.23026', 1, {'main.tex': TEX})
    grouped = publications_with_records(root, limit=1)
    assert [paper for paper, _ in grouped] == ['2410.23025']
    assert len(grouped[0][1]) == 2


def test_adapter_ignores_non_paper_directories(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': TEX})
    (root / 'data' / 'history').mkdir(parents=True)
    (root / 'data' / 'not-a-paper').mkdir(parents=True)
    assert [paper for paper, _ in publications_with_records(root)] == ['2410.23025']
