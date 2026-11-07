"""Prepare reproducible, small M1 report inputs from a frozen crawler export.

The export is authoritative for dataset counts. Runtime roots contribute only
per-session performance data; they are never merged into the dataset ledger.
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

from .storage import atomic_json, digest


def _rows(path):
    with path.open(newline='', encoding='utf8') as stream:
        return list(csv.DictReader(stream))


def _numbers(rows, field):
    return [float(row[field]) for row in rows if row.get(field) not in (None, '')]


def _mean(values):
    return sum(values) / len(values) if values else None


def _iso(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def prepare_report(export_dir, runtime_roots, output_dir, *, scan_storage=False):
    """Validate one export and write aggregate-only report inputs.

    Multiple runtime roots may contain copies of the same run; run_id is the
    deduplication key. The first root is canonical for optional final disk size.
    """
    export_dir = Path(export_dir)
    runtime_roots = list(dict.fromkeys(Path(root) for root in runtime_roots))
    output_dir = Path(output_dir)
    manifest = json.loads((export_dir / 'manifest.json').read_text(encoding='utf8'))
    if manifest.get('schema_version') != 3:
        raise ValueError('Expected a schema 3 export; create a fresh export')
    for name, expected in manifest['checksums'].items():
        if Path(name).name != name or digest(export_dir / name) != expected:
            raise ValueError(f'Export checksum mismatch: {name}')

    papers = _rows(export_dir / 'paper_metrics.csv')
    versions = _rows(export_dir / 'version_metrics.csv')
    if versions and 'metadata_status' not in versions[0]:
        raise ValueError('version_metrics.csv lacks metadata_status; create a fresh export')
    snapshots = pq.read_table(export_dir / 'reference_snapshots.parquet').to_pylist()
    version_snapshots = [row for row in snapshots if row['version'] > 0]
    valid_refs = [row['reference_count'] for row in version_snapshots
                  if row['status'] == 'succeeded' and row['reference_count'] is not None]
    paper_reference_counts = _numbers(
        [row for row in papers if row.get('metadata_exported') == 'True'],
        'reference_count')
    source_sizes = {field: _numbers(versions, field)
                    for field in ('compressed_bytes', 'unpacked_bytes', 'kept_bytes')}
    source_status = Counter(row['source_status'] for row in versions)
    metadata_status = Counter(row.get('metadata_status', 'unknown') for row in versions)
    ref_status = Counter(row['status'] for row in version_snapshots)
    discovery_status = Counter(row['discovery_status'] for row in papers)

    runs = {row['run_id']: row for row in json.loads(
        (export_dir / 'run_metrics.json').read_text(encoding='utf8')) if row.get('run_id')}
    allowed_run_ids = set(manifest.get('contributing_run_ids') or runs)
    runs = {run_id: row for run_id, row in runs.items() if run_id in allowed_run_ids}
    run_dirs = {}
    ignored_run_ids = set()
    for root in runtime_roots:
        for directory in sorted((root / 'metrics').glob('*')):
            if not directory.is_dir():
                continue
            summary_path = directory / 'summary.json'
            checkpoint_path = directory / 'checkpoint.json'
            path = summary_path if summary_path.exists() else checkpoint_path
            if not path.exists():
                continue
            row = json.loads(path.read_text(encoding='utf8'))
            run_id = row.get('run_id')
            if run_id and run_id not in allowed_run_ids:
                ignored_run_ids.add(run_id)
            if not run_id or run_id not in allowed_run_ids:
                continue
            if run_id in run_dirs:
                previous = run_dirs[run_id]
                previous_path = previous / ('summary.json' if (previous / 'summary.json').exists()
                                            else 'checkpoint.json')
                if digest(previous_path) != digest(path):
                    raise ValueError(f'Conflicting copies of metrics run {run_id}')
                continue
            if run_id not in runs or summary_path.exists():
                runs[run_id] = row
                run_dirs[run_id] = directory
    wall = [float(row['wall_seconds']) for row in runs.values()
            if row.get('wall_seconds') is not None]
    peaks = [int(row['rss_peak_bytes']) for row in runs.values()
             if row.get('rss_peak_bytes') is not None]
    scopes = {row.get('disk_measurement_scope') for row in runs.values()
              if row.get('disk_observed_peak_bytes') is not None}
    disk_peaks = [row['disk_observed_peak_bytes'] for row in runs.values()
                  if row.get('disk_observed_peak_bytes') is not None]

    rss_area = 0.0
    rss_duration = 0.0
    per_paper = defaultdict(float)
    discovery_spans = []
    for run_id, directory in run_dirs.items():
        resources = directory / 'resources.csv'
        if resources.exists():
            samples = _rows(resources)
            for earlier, later in zip(samples, samples[1:]):
                duration = float(later['elapsed_seconds']) - float(earlier['elapsed_seconds'])
                if duration > 0:
                    rss_area += float(earlier['rss_bytes']) * duration
                    rss_duration += duration
        paper_file = directory / 'paper_metrics.csv'
        if paper_file.exists():
            for row in _rows(paper_file):
                per_paper[row['arxiv_id']] += float(row['elapsed_seconds'])
        log_file = directory.parent.parent / 'logs' / run_id / 'events.jsonl'
        if log_file.exists():
            elapsed = []
            with log_file.open(encoding='utf8') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # Interrupted final line.
                    jobs = event.get('jobs') or [event.get('job')]
                    if any(job and len(job) >= 3 and job[2] == 'discovery' for job in jobs):
                        elapsed.append(float(event['elapsed_seconds']))
            if elapsed:
                discovery_spans.append(max(elapsed) - min(elapsed))

    starts = [_iso(row.get('started_at')) for row in runs.values()]
    ends = [_iso(row.get('finished_at') or row.get('last_sample_at')) for row in runs.values()]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    active_wall = sum(wall) if wall else None
    final_data_bytes = None
    if scan_storage:
        if not runtime_roots:
            raise ValueError('A canonical runtime root is required for a storage scan')
        data_root = runtime_roots[0] / 'data'
        if not data_root.is_dir():
            raise ValueError(f'Canonical runtime has no data directory: {data_root}')
        final_data_bytes = sum(path.stat().st_size for path in data_root.rglob('*')
                               if path.is_file())

    assigned = manifest['assigned_ids']
    export_logical_bytes = sum(path.stat().st_size for path in export_dir.iterdir()
                               if path.is_file())
    def rate(numerator, denominator):
        return numerator / denominator if denominator else None
    output = {
        'provenance': {'export_run_id': manifest.get('run_id'),
                       'export_manifest_sha256': digest(export_dir / 'manifest.json'),
                       'run_ids': sorted(runs), 'runtime_roots_count': len(runtime_roots),
                       'ignored_run_ids_not_in_export': sorted(ignored_run_ids),
                       'run_ids_without_local_resources': sorted(set(runs) - set(run_dirs))},
        'coverage': {'assigned_papers': assigned, 'exported_papers': manifest['papers'],
                     'paper_export_rate': rate(manifest['papers'], assigned),
                     'discovery_status': dict(discovery_status),
                     'metadata_versions_total': len(versions),
                     'metadata_versions_succeeded': metadata_status['succeeded'],
                     'metadata_version_success_rate': rate(metadata_status['succeeded'], len(versions)),
                     'metadata_status': dict(metadata_status),
                     'source_versions_total': len(versions),
                     'source_coverage_rate': rate(source_status['succeeded'] + source_status['no_tex'], len(versions)),
                     'tex_availability_rate': rate(source_status['succeeded'], len(versions)),
                     'source_status': dict(source_status),
                     'reference_versions_total': len(version_snapshots),
                     'reference_versions_succeeded': ref_status['succeeded'],
                     'reference_version_success_rate': rate(ref_status['succeeded'], len(version_snapshots)),
                     'reference_status': dict(ref_status)},
        'source': {f'mean_{field}': _mean(values) for field, values in source_sizes.items()}
                  | {f'{field}_sample_count': len(values) for field, values in source_sizes.items()},
        'references': {'mean_per_successful_version': _mean(valid_refs),
                       'successful_version_sample_count': len(valid_refs),
                       'mean_paper_union_with_snapshot': _mean(paper_reference_counts),
                       'paper_union_sample_count': len(paper_reference_counts),
                       'legacy_snapshots_excluded': len(snapshots) - len(version_snapshots),
                       'paper_union_rows_with_arxiv_id': manifest['references']},
        'performance': {'sessions': len(runs), 'active_wall_seconds': active_wall,
                        'mean_active_seconds_per_assigned_paper':
                            active_wall / assigned if active_wall is not None and assigned else None,
                        'calendar_elapsed_seconds': (max(ends) - min(starts)).total_seconds()
                            if starts and ends else None,
                        'rss_peak_bytes': max(peaks) if peaks else None,
                        'rss_time_weighted_mean_bytes': rss_area / rss_duration if rss_duration else None,
                        'rss_sampled_seconds': rss_duration,
                        'disk_observed_peak_bytes': max(disk_peaks) if len(scopes) == 1 and disk_peaks else None,
                        'disk_peak_scope': next(iter(scopes)) if len(scopes) == 1 else None,
                        'final_data_logical_bytes': final_data_bytes,
                        'export_snapshot_logical_bytes': export_logical_bytes,
                        'paper_observed_span_mean_seconds': _mean(list(per_paper.values())),
                        'paper_observed_span_sample_count': len(per_paper),
                        'discovery_observed_span_seconds': sum(discovery_spans) if discovery_spans else None},
        'limitations': [
            'Version reference correspondence with arXiv revisions is unverified.',
            'Source unpacked_bytes includes all archive members; kept_bytes includes only .tex/.bib.',
            'Session disk peak is touched-path logical bytes, not historical whole-dataset peak.',
            'Canonical data tree size includes retained caches/history and is a final-state measurement, not peak usage.',
            'Per-paper observed spans may include waiting and overlap across workers; active wall / assigned papers is the system-wide average.',
            'Discovery observed span may include waits and concurrent S2 work.',
            'RSS peaks are sampled; interrupted sessions without resources.csv are missing from the weighted mean.',
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / 'summary.json', output)
    atomic_json(output_dir / 'distributions.json', {
        'source_sizes': source_sizes,
        'references_per_successful_version': valid_refs,
        'paper_observed_spans_seconds': list(per_paper.values()),
    })
    return output


def prepare_combined_report(parts, output_dir, *, scan_storage=False):
    """Combine disjoint, independently exported ranges without merging ledgers."""
    from tempfile import TemporaryDirectory

    if len(parts) < 2:
        raise ValueError('At least two export/runtime pairs are required')
    summaries = []
    distributions = []
    all_ids = set()
    run_ids = set()
    starts, ends = [], []
    for export_dir, runtime_root in parts:
        export_dir, runtime_root = Path(export_dir), Path(runtime_root)
        # Validate before publishing any combined report file.
        with TemporaryDirectory() as temporary:
            summary = prepare_report(export_dir, [runtime_root], temporary,
                                     scan_storage=scan_storage)
            distribution = json.loads((Path(temporary) / 'distributions.json').read_text())
        rows = _rows(export_dir / 'paper_metrics.csv')
        paper_ids = [row['arxiv_id'] for row in rows]
        if len(set(paper_ids)) != summary['coverage']['assigned_papers']:
            raise ValueError(f'Export does not enumerate its full assigned range: {export_dir}')
        overlap = all_ids.intersection(paper_ids)
        if overlap:
            raise ValueError(f'Overlapping arXiv IDs across exports: {sorted(overlap)[:5]}')
        all_ids.update(paper_ids)
        runs = set(summary['provenance']['run_ids'])
        if run_ids.intersection(runs):
            raise ValueError(f'Duplicate crawler run IDs across exports: {sorted(run_ids & runs)}')
        run_ids.update(runs)
        for row in json.loads((export_dir / 'run_metrics.json').read_text()):
            if row.get('run_id') not in runs:
                continue
            start = _iso(row.get('started_at'))
            end = _iso(row.get('finished_at') or row.get('last_sample_at'))
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
        summaries.append(summary)
        distributions.append(distribution)

    def total_at(*keys):
        values = [item for summary in summaries
                  if (item := _get(summary, keys)) is not None]
        return sum(values) if values else None

    def weighted_mean(mean_key, count_key, section):
        count = total_at(section, count_key)
        if not count:
            return None
        return sum(summary[section][mean_key] * summary[section][count_key]
                   for summary in summaries if summary[section][count_key]) / count

    def combined_status(key):
        result = Counter()
        for summary in summaries:
            result.update(summary['coverage'][key])
        return dict(result)

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    coverage = {
        key: total_at('coverage', key) for key in (
            'assigned_papers', 'exported_papers', 'metadata_versions_total',
            'metadata_versions_succeeded', 'source_versions_total',
            'reference_versions_total', 'reference_versions_succeeded')
    }
    for key in ('discovery_status', 'metadata_status', 'source_status', 'reference_status'):
        coverage[key] = combined_status(key)
    coverage['paper_export_rate'] = ratio(coverage['exported_papers'], coverage['assigned_papers'])
    coverage['metadata_version_success_rate'] = ratio(
        coverage['metadata_versions_succeeded'], coverage['metadata_versions_total'])
    coverage['source_coverage_rate'] = ratio(
        coverage['source_status'].get('succeeded', 0) + coverage['source_status'].get('no_tex', 0),
        coverage['source_versions_total'])
    coverage['tex_availability_rate'] = ratio(
        coverage['source_status'].get('succeeded', 0), coverage['source_versions_total'])
    coverage['reference_version_success_rate'] = ratio(
        coverage['reference_versions_succeeded'], coverage['reference_versions_total'])

    source = {}
    for field in ('compressed_bytes', 'unpacked_bytes', 'kept_bytes'):
        source[f'{field}_sample_count'] = total_at('source', f'{field}_sample_count')
        source[f'mean_{field}'] = weighted_mean(
            f'mean_{field}', f'{field}_sample_count', 'source')
    references = {
        key: total_at('references', key) for key in (
            'successful_version_sample_count', 'paper_union_sample_count',
            'legacy_snapshots_excluded', 'paper_union_rows_with_arxiv_id')
    }
    for mean_key, count_key in (
        ('mean_per_successful_version', 'successful_version_sample_count'),
        ('mean_paper_union_with_snapshot', 'paper_union_sample_count'),
    ):
        references[mean_key] = weighted_mean(mean_key, count_key, 'references')

    performance = {
        key: total_at('performance', key) for key in (
            'sessions', 'active_wall_seconds', 'rss_sampled_seconds',
            'final_data_logical_bytes', 'export_snapshot_logical_bytes',
            'paper_observed_span_sample_count', 'discovery_observed_span_seconds')
    }
    performance['mean_active_seconds_per_assigned_paper'] = ratio(
        performance['active_wall_seconds'], coverage['assigned_papers'])
    performance['calendar_elapsed_seconds'] = (
        (max(ends) - min(starts)).total_seconds() if starts and ends else None)
    for key in ('rss_peak_bytes',):
        values = [s['performance'][key] for s in summaries if s['performance'][key] is not None]
        performance[key] = max(values) if values else None
    rss_seconds = performance['rss_sampled_seconds']
    performance['rss_time_weighted_mean_bytes'] = (
        sum(s['performance']['rss_time_weighted_mean_bytes'] * s['performance']['rss_sampled_seconds']
            for s in summaries if s['performance']['rss_time_weighted_mean_bytes'] is not None)
        / rss_seconds if rss_seconds else None)
    performance['paper_observed_span_mean_seconds'] = weighted_mean(
        'paper_observed_span_mean_seconds', 'paper_observed_span_sample_count', 'performance')
    scopes = {s['performance']['disk_peak_scope'] for s in summaries
              if s['performance']['disk_observed_peak_bytes'] is not None}
    peaks = [s['performance']['disk_observed_peak_bytes'] for s in summaries
             if s['performance']['disk_observed_peak_bytes'] is not None]
    performance['disk_peak_scope'] = next(iter(scopes)) if len(scopes) == 1 else None
    performance['disk_observed_peak_bytes'] = max(peaks) if len(scopes) == 1 and peaks else None

    combined_distributions = {
        key: [value for distribution in distributions for value in distribution[key]]
        for key in ('references_per_successful_version', 'paper_observed_spans_seconds')
    }
    combined_distributions['source_sizes'] = {
        field: [value for distribution in distributions for value in distribution['source_sizes'][field]]
        for field in ('compressed_bytes', 'unpacked_bytes', 'kept_bytes')
    }
    output = {
        'provenance': {
            'parts': [dict(export_run_id=s['provenance']['export_run_id'],
                           export_manifest_sha256=s['provenance']['export_manifest_sha256'],
                           assigned_papers=s['coverage']['assigned_papers'],
                           run_ids=s['provenance']['run_ids']) for s in summaries],
            'run_ids': sorted(run_ids),
            'run_ids_without_local_resources': sorted({run_id for s in summaries
                for run_id in s['provenance']['run_ids_without_local_resources']}),
        },
        'coverage': coverage, 'source': source, 'references': references,
        'performance': performance,
        'limitations': summaries[0]['limitations'] + [
            'Independent runtime ledgers were not merged; aggregate counts use non-overlapping assigned ID sets.',
            'Summed active wall time is total session time across runtimes and may include concurrent sessions.',
            'Observed disk peaks are per-runtime maxima, not a combined historical peak.',
        ],
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / 'summary.json', output)
    atomic_json(output_dir / 'distributions.json', combined_distributions)
    return output


def _get(mapping, keys):
    for key in keys:
        mapping = mapping[key]
    return mapping


def main():
    parser = argparse.ArgumentParser(description='Prepare small, reproducible M1 report inputs')
    parser.add_argument('--export', type=Path)
    parser.add_argument('--runtime', type=Path, action='append', default=[])
    parser.add_argument('--part', nargs=2, action='append', metavar=('EXPORT', 'RUNTIME'),
                        help='One independent export and its runtime; repeat for disjoint ranges')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scan-storage', action='store_true',
                        help='Scan logical bytes under each selected runtime/data directory')
    args = parser.parse_args()
    if args.part:
        if args.export or args.runtime:
            parser.error('--part cannot be combined with --export/--runtime')
        result = prepare_combined_report(args.part, args.output,
                                         scan_storage=args.scan_storage)
    else:
        if not args.export:
            parser.error('--export is required unless --part is used')
        result = prepare_report(args.export, args.runtime, args.output,
                                scan_storage=args.scan_storage)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
