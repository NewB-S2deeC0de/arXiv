import json

import pytest

from parser.dataset_snapshot import prepare_snapshot
from parser.benchmark import benchmark
from parser.benchmark_report import write_report


def test_four_member_snapshot_and_real_benchmark(tmp_path, make_source):
    roots = [tmp_path / f'member{i}' for i in range(4)]
    for index, root in enumerate(roots):
        make_source(root, f'2504.{index:05}', 1, {
            'paper.tex': r'\documentclass{article}\begin{document}Shared sentence.\end{document}'})
    before = {str(p): p.read_bytes() for root in roots for p in root.rglob('*') if p.is_file()}
    output = tmp_path / 'snapshot'
    snapshot = prepare_snapshot(roots, output)
    assert snapshot['status'] == 'completed'
    assert len(snapshot['versions']) == 4
    result = benchmark(output, tmp_path / 'benchmark')
    assert result['papers_processed'] == 4
    assert result['versions']['versions_parsed'] == 4
    assert result['acceptance'] == 'requires_D1_review'
    assert before == {str(p): p.read_bytes() for root in roots for p in root.rglob('*') if p.is_file()}
    assert json.loads((output / 'snapshot.json').read_text()) == snapshot
    report = write_report(tmp_path / 'benchmark', tmp_path / 'chapter-results.md').read_text(encoding='utf8')
    assert '| papers_processed | 4 |' in report
    assert snapshot['fingerprint'] in report
    assert 'SHA-256' in report


def test_duplicate_versions_are_counted_once_or_rejected(tmp_path, make_source):
    roots = [tmp_path / 'a', tmp_path / 'b']
    for root in roots:
        make_source(root, '2504.00001', 1, {'paper.tex': 'same bytes'})
    snapshot = prepare_snapshot(roots, tmp_path / 'snapshot')
    assert len(snapshot['versions']) == 1
    assert len(snapshot['versions'][0]['owners']) == 2
    (roots[1] / 'data/2504.00001/versions/v1/source/tex/paper.tex').write_text('different')
    with pytest.raises(ValueError, match='conflicting duplicate'):
        prepare_snapshot(roots, tmp_path / 'conflict')
    assert not (tmp_path / 'conflict').exists()


def test_snapshot_rejects_input_overlap(tmp_path, make_source):
    make_source(tmp_path, '2504.00001', 1, {})
    with pytest.raises(ValueError, match='separate'):
        prepare_snapshot([tmp_path], tmp_path / 'snapshot')


def test_real_benchmark_quality_counts_are_not_occurrences(tmp_path, make_source):
    root = tmp_path / 'runtime'
    for version, title in ((1, 'First title'), (2, 'Different title')):
        make_source(root, '2504.00001', version, {
            'main.tex': r'\documentclass{article}\begin{document}'
                        r'Use \cite{ghost}. Again \cite{ghost,known}. '
                        r'\bibliography{refs}\end{document}',
            'refs.bib': '@article{known, title={' + title + '}, doi={10.1234/shared}}',
        })
    result = benchmark(root, tmp_path / 'run')
    assert result['versions']['versions_parsed'] == 2
    assert result['unresolved_citations'] == 1
    assert result['bibliography_conflicts'] == 1
    assert result['bibliography_conflict_fields'] == 1
    assert result['quality_source'] == 'C3 manifest.quality'


def test_partial_member_data_is_never_official_group_acceptance(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2504.00001', 1, {
        'main.tex': r'\documentclass{article}\begin{document}Sentence.\end{document}'})
    result = benchmark(root, tmp_path / 'run', group_data_complete=False)
    assert result['status'] == 'completed'
    assert result['acceptance'] == 'blocked_incomplete_group_dataset'
    assert result['group_data_complete'] is False
