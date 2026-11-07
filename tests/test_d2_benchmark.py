import json

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from parser.benchmark import benchmark, validate_coverage


@pytest.fixture
def successful_parser(monkeypatch):
    """Scaffold for reporting tests only, never real-parser acceptance."""
    def install(*, papers=1, parsed=1, pending=0, failed=0):
        def run(source, *, output_root, **kwargs):
            output_root.mkdir()
            pq.write_table(pa.table({'kind': pa.array([], type=pa.string())}),
                           output_root / 'elements.parquet')
            return dict(papers_processed=papers, elements=0, bibtex=0,
                        input_fingerprints={}, versions=dict(
                            versions_total=parsed + pending + failed,
                            versions_parsed=parsed, versions_pending=pending,
                            versions_failed=failed, versions_empty=0,
                            versions_partial=0, missing=0, gather_failed=0))
        monkeypatch.setattr('parser.benchmark.run_parser', run)
    return install


def test_parquet_failure_after_parser_success_is_reported(tmp_path, monkeypatch, successful_parser):
    source = tmp_path / 'input'
    source.mkdir()
    output = tmp_path / 'run'
    successful_parser()

    def corrupt(*args, **kwargs):
        raise OSError('corrupt parquet')

    monkeypatch.setattr('parser.benchmark.pq.ParquetFile', corrupt)
    with pytest.raises(OSError, match='corrupt parquet'):
        benchmark(source, output)
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['status'] == 'failed'
    assert summary['error_type'] == 'OSError'
    assert 'acceptance' not in summary
    assert (output / 'resources.csv').exists()


@pytest.mark.parametrize('papers,parsed,pending,failed,acceptance', [
    (0, 0, 0, 0, 'blocked_no_publications'),
    (1, 0, 0, 0, 'blocked_no_parseable_versions'),
    (1, 0, 1, 0, 'blocked'),
    (1, 1, 0, 1, 'blocked'),
    (1, 1, 0, 0, 'requires_D1_review'),
])
def test_benchmark_sample_acceptance(tmp_path, successful_parser,
                                     papers, parsed, pending, failed, acceptance):
    source = tmp_path / 'empty_m1_runtime'
    source.mkdir()
    successful_parser(papers=papers, parsed=parsed, pending=pending, failed=failed)
    output = tmp_path / 'run'
    result = benchmark(source, output)
    assert result['status'] == 'completed'
    assert result['acceptance'] == acceptance
    assert json.loads((output / 'summary.json').read_text()) == result
    if papers == 0:
        assert result['seconds_per_publication'] is None
    assert list(source.iterdir()) == []


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


@pytest.mark.parametrize('overrides,message', [
    ({'versions_total': 8}, 'partition mismatch'),
    ({'gather_failed': 2}, 'subset'),
    ({'missing': -1}, 'invalid coverage counter'),
    ({'versions_parsed': True}, 'invalid coverage counter'),
])
def test_coverage_rejects_inconsistent_counters(overrides, message):
    versions = dict(versions_total=6, versions_parsed=1, versions_empty=1,
                    versions_partial=1, versions_pending=1, versions_failed=1,
                    missing=1, gather_failed=1)
    versions.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_coverage(versions)


def test_coverage_does_not_double_count_gather_failures():
    validate_coverage(dict(versions_total=6, versions_parsed=1, versions_empty=1,
                          versions_partial=1, versions_pending=1, versions_failed=1,
                          missing=1, gather_failed=1))


def test_invalid_coverage_is_saved_as_failed_run(tmp_path, monkeypatch, successful_parser):
    source = tmp_path / 'input'
    source.mkdir()
    successful_parser()
    from parser import benchmark as module
    original = module.run_parser

    def inconsistent(*args, **kwargs):
        manifest = original(*args, **kwargs)
        manifest['versions']['versions_total'] += 1
        return manifest

    monkeypatch.setattr(module, 'run_parser', inconsistent)
    output = tmp_path / 'run'
    with pytest.raises(ValueError, match='partition mismatch'):
        benchmark(source, output)
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['status'] == 'failed'
    assert summary['error_type'] == 'ValueError'
    assert summary['seconds_per_publication'] is None
    assert 'acceptance' not in summary
