import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from crawler.m1_report import prepare_report, prepare_combined_report
from crawler.storage import digest


def _csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_report_uses_version_snapshots_and_unique_session_metrics(tmp_path):
    export = tmp_path / 'export'
    export.mkdir()
    _csv(export / 'paper_metrics.csv', [
        dict(arxiv_id='a', discovery_status='succeeded', metadata_exported='True', reference_count='3'),
        dict(arxiv_id='b', discovery_status='failed', metadata_exported='False', reference_count=''),
    ])
    _csv(export / 'version_metrics.csv', [
        dict(arxiv_id='a', version='1', source_status='succeeded', references_status='succeeded',
             metadata_status='succeeded', compressed_bytes='4', unpacked_bytes='10', kept_bytes='2'),
        dict(arxiv_id='a', version='2', source_status='no_tex', references_status='failed',
             metadata_status='failed', compressed_bytes='6', unpacked_bytes='20', kept_bytes='0'),
    ])
    pq.write_table(pa.Table.from_pylist([
        dict(arxiv_id='a', version=0, status='succeeded', reference_count=90),
        dict(arxiv_id='a', version=1, status='succeeded', reference_count=3),
        dict(arxiv_id='a', version=2, status='failed', reference_count=None),
    ]), export / 'reference_snapshots.parquet')
    (export / 'run_metrics.json').write_text(json.dumps([
        dict(run_id='r1', wall_seconds=10, rss_peak_bytes=100,
             disk_observed_peak_bytes=500, disk_measurement_scope='session_touched_paths',
             started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:10+00:00'),
    ]))
    checksums = {p.name: digest(p) for p in export.iterdir()}
    (export / 'manifest.json').write_text(json.dumps(dict(schema_version=3,
        assigned_ids=2, papers=1, references=1, contributing_run_ids=['r1'], checksums=checksums)))
    runtime = tmp_path / 'runtime'
    metrics = runtime / 'metrics' / 'r1'
    metrics.mkdir(parents=True)
    (metrics / 'summary.json').write_text((export / 'run_metrics.json').read_text()[1:-1])
    _csv(metrics / 'resources.csv', [
        dict(elapsed_seconds=0, rss_bytes=80, disk_bytes=100),
        dict(elapsed_seconds=5, rss_bytes=100, disk_bytes=500),
        dict(elapsed_seconds=10, rss_bytes=100, disk_bytes=500),
    ])
    _csv(metrics / 'paper_metrics.csv', [
        dict(arxiv_id='a', run_id='r1', elapsed_seconds=7, complete='True'),
    ])
    output = tmp_path / 'report-data'
    summary = prepare_report(export, [runtime, runtime], output)
    assert summary['coverage']['assigned_papers'] == 2
    assert summary['coverage']['exported_papers'] == 1
    assert summary['coverage']['reference_versions_succeeded'] == 1
    assert summary['coverage']['reference_versions_total'] == 2
    assert summary['coverage']['metadata_version_success_rate'] == .5
    assert summary['coverage']['reference_version_success_rate'] == .5
    assert summary['references']['mean_per_successful_version'] == 3
    assert summary['references']['mean_paper_union_with_snapshot'] == 3
    assert summary['references']['legacy_snapshots_excluded'] == 1
    assert summary['source']['mean_unpacked_bytes'] == 15
    assert summary['source']['mean_kept_bytes'] == 1
    assert summary['performance']['active_wall_seconds'] == 10
    assert summary['performance']['rss_time_weighted_mean_bytes'] == 90
    assert summary['performance']['rss_peak_bytes'] == 100
    assert summary['performance']['paper_observed_span_sample_count'] == 1
    assert summary['performance']['disk_peak_scope'] == 'session_touched_paths'
    assert summary['performance']['export_snapshot_logical_bytes'] > 0
    assert (output / 'summary.json').exists()
    assert (output / 'distributions.json').exists()


def test_report_rejects_modified_export(tmp_path):
    import pytest
    export = tmp_path / 'export'; export.mkdir()
    (export / 'paper_metrics.csv').write_text('original')
    (export / 'manifest.json').write_text(json.dumps(dict(schema_version=3,
        checksums={'paper_metrics.csv': digest(export / 'paper_metrics.csv')})))
    (export / 'paper_metrics.csv').write_text('changed')
    with pytest.raises(ValueError, match='checksum mismatch'):
        prepare_report(export, [], tmp_path / 'output')


def test_combined_disjoint_exports_weight_means_and_reject_overlap(tmp_path):
    import pytest

    def part(name, ids, source_bytes, refs, wall):
        export = tmp_path / name / 'exports' / 'final'
        export.mkdir(parents=True)
        _csv(export / 'paper_metrics.csv', [
            dict(arxiv_id=paper, discovery_status='succeeded', metadata_exported='True',
                 reference_count=str(refs)) for paper in ids])
        _csv(export / 'version_metrics.csv', [
            dict(arxiv_id=paper, version='1', metadata_status='succeeded',
                 source_status='succeeded', compressed_bytes=str(source_bytes),
                 unpacked_bytes=str(source_bytes * 2), kept_bytes=str(source_bytes))
            for paper in ids])
        pq.write_table(pa.Table.from_pylist([
            dict(arxiv_id=paper, version=1, status='succeeded', reference_count=refs)
            for paper in ids]), export / 'reference_snapshots.parquet')
        (export / 'run_metrics.json').write_text(json.dumps([dict(
            run_id=name, wall_seconds=wall, rss_peak_bytes=source_bytes,
            rss_time_weighted_mean_bytes=source_bytes,
            started_at='2026-01-01T00:00:00+00:00',
            finished_at=f'2026-01-01T00:00:{wall:02d}+00:00')]))
        (export / 'manifest.json').write_text(json.dumps(dict(
            schema_version=3, assigned_ids=len(ids), papers=len(ids), references=len(ids)*refs,
            contributing_run_ids=[name],
            checksums={path.name: digest(path) for path in export.iterdir()})))
        return export, tmp_path / name

    first = part('first', ['a'], 10, 2, 10)
    second = part('second', ['b', 'c'], 40, 5, 20)
    output = tmp_path / 'report'
    result = prepare_combined_report([first, second], output)
    assert result['coverage']['assigned_papers'] == 3
    assert result['coverage']['metadata_versions_succeeded'] == 3
    assert result['coverage']['reference_version_success_rate'] == 1
    assert result['source']['mean_unpacked_bytes'] == 60
    assert result['references']['mean_per_successful_version'] == 4
    assert result['performance']['active_wall_seconds'] == 30
    assert result['performance']['calendar_elapsed_seconds'] == 20
    assert result['provenance']['run_ids'] == ['first', 'second']
    assert len(json.loads((output / 'distributions.json').read_text())['source_sizes']['kept_bytes']) == 3

    overlap = part('overlap', ['a'], 1, 1, 1)
    before = (output / 'summary.json').read_bytes()
    with pytest.raises(ValueError, match='Overlapping arXiv IDs'):
        prepare_combined_report([first, overlap], output)
    assert (output / 'summary.json').read_bytes() == before
