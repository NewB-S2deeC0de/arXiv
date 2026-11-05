import json

import pytest

from parser.benchmark import benchmark


def test_failed_real_run_keeps_telemetry_and_propagates_error(tmp_path, monkeypatch):
    source = tmp_path / 'input'
    source.mkdir()
    output = tmp_path / 'run'

    def failed(*args, **kwargs):
        raise RuntimeError('dependency unavailable')

    monkeypatch.setattr('parser.benchmark.run_parser', failed)
    with pytest.raises(RuntimeError, match='dependency unavailable'):
        benchmark(source, output, sample_seconds=0.01)
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['status'] == 'failed'
    assert summary['error_type'] == 'RuntimeError'
    assert summary['unresolved_citations'] is None
    assert summary['seconds_per_publication'] is None
    assert summary['observed_peak_rss_bytes'] > 0
    assert (output / 'resources.csv').read_text().startswith('elapsed_seconds,rss_bytes')
    assert list(source.iterdir()) == []


def test_benchmark_rejects_input_overlap_and_existing_output(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    with pytest.raises(ValueError, match='separate'):
        benchmark(source, source / 'result')
    output = tmp_path / 'existing'
    output.mkdir()
    with pytest.raises(ValueError, match='new output'):
        benchmark(source, output)
