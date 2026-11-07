"""Fresh CPU parser benchmark. Real services only; never fabricates missing metrics."""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import threading
import time
from datetime import datetime, timezone

import psutil
import pyarrow.parquet as pq

from .pipeline import run_parser


def validate_coverage(versions):
    """BC-06: gather_failed is a cause subset, not another partition bucket."""
    partition = ('versions_parsed', 'versions_empty', 'versions_partial',
                 'versions_pending', 'versions_failed', 'missing')
    for key in ('versions_total', *partition, 'gather_failed'):
        value = versions[key]
        if type(value) is not int or value < 0:
            raise ValueError(f'invalid coverage counter: {key}')
    if versions['versions_total'] != sum(versions[key] for key in partition):
        raise ValueError('BC-06 version coverage partition mismatch')
    if versions['gather_failed'] > versions['versions_failed']:
        raise ValueError('BC-06 gather_failed must be a subset of versions_failed')


def benchmark(persistent_root, output_root, *, limit=None, sample_seconds=0.25,
              include_partial=False, group_data_complete=None):
    if sample_seconds <= 0:
        raise ValueError('sample_seconds must be positive')
    source, output = Path(persistent_root).resolve(), Path(output_root).resolve()
    if not source.is_dir():
        raise ValueError('persistent root must exist')
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('benchmark output must be separate from the M1 input tree')
    if output.exists():
        raise ValueError('use a new output directory for each fresh benchmark')
    output.mkdir(parents=True)
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    packages = {p: importlib.metadata.version(p) for p in ('psutil', 'pyarrow', 'pytest')}
    environment = dict(python=platform.python_version(), platform=platform.platform(),
                       cpu=platform.processor(), logical_cpus=psutil.cpu_count(),
                       total_ram_bytes=psutil.virtual_memory().total, packages=packages,
                       commit=commit, dirty=dirty, started_at=datetime.now(timezone.utc).isoformat(),
                       input=str(source), limit=limit, include_partial=include_partial,
                       group_data_complete=group_data_complete,
                       sample_seconds=sample_seconds, mode='fresh', hardware_verification='operator must confirm Colab CPU-only')
    (output / 'environment.json').write_text(json.dumps(environment, indent=2), encoding='utf8')
    stop = threading.Event()
    samples = []
    process = psutil.Process()
    started = time.perf_counter()

    def sample():
        samples.append((time.perf_counter() - started, process.memory_info().rss))

    def monitor():
        while not stop.wait(sample_seconds):
            sample()

    sample()
    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    result = dict(status='failed', unresolved_citations=None, bibliography_conflicts=None,
                  group_data_complete=group_data_complete)
    try:
        manifest = run_parser(source, output_root=output / 'parsed', limit=limit,
                              resume=False, include_partial=include_partial)
        validate_coverage(manifest['versions'])
        result.update(versions=manifest['versions'],
                      elements=manifest['elements'], bibtex=manifest['bibtex'],
                      input_fingerprints=manifest['input_fingerprints'])
        quality = manifest.get('quality', {})
        result.update(
            unresolved_citations=quality.get('citations_unresolved'),
            bibliography_conflicts=quality.get('bibtex_conflicts'),
            bibliography_conflict_fields=quality.get('bibtex_conflict_fields'),
            quality_units=dict(
                unresolved_citations='sum of distinct unresolved keys per publication in exported elements',
                bibliography_conflicts='canonical records with at least one conflicting field',
                bibliography_conflict_fields='canonical record/field pairs with conflicts'),
            quality_source='C3 manifest.quality' if quality else None,
            validation=manifest.get('validation'),
        )
        snapshot_path = source / 'snapshot.json'
        if snapshot_path.is_file():
            snapshot = json.loads(snapshot_path.read_text(encoding='utf8'))
            if snapshot.get('status') != 'completed':
                raise ValueError('benchmark requires a completed dataset snapshot')
            result['dataset_snapshot'] = dict(fingerprint=snapshot['fingerprint'],
                                              members=snapshot['members'])
        kinds = {}
        for batch in pq.ParquetFile(output / 'parsed' / 'elements.parquet').iter_batches(columns=['kind']):
            for kind in batch.column(0).to_pylist():
                kinds[kind] = kinds.get(kind, 0) + 1
        result['unique_elements_by_kind'] = kinds
        result['papers_processed'] = manifest['papers_processed']
        if manifest['papers_processed'] == 0:
            result['acceptance'] = 'blocked_no_publications'
        elif (manifest['versions']['versions_pending'] or manifest['versions']['versions_failed']
              or manifest.get('validation', {}).get('ok') is False):
            result['acceptance'] = 'blocked'
        elif manifest['versions']['versions_parsed'] == 0:
            result['acceptance'] = 'blocked_no_parseable_versions'
        elif group_data_complete is False:
            result['acceptance'] = 'blocked_incomplete_group_dataset'
        else:
            result['acceptance'] = 'requires_D1_review'
        result['status'] = 'completed'
        return result
    except Exception as exc:
        result['status'] = 'failed'
        result['error_type'] = type(exc).__name__
        raise
    finally:
        stop.set()
        worker.join()
        sample()
        result['wall_seconds'] = time.perf_counter() - started
        count = result.get('papers_processed', 0)
        result['seconds_per_publication'] = result['wall_seconds'] / count if count and result['status'] == 'completed' else None
        result['observed_peak_rss_bytes'] = max(rss for _, rss in samples)
        result['parsed_output_bytes'] = sum(p.stat().st_size for p in (output / 'parsed').rglob('*') if p.is_file())
        result['measurement_note'] = ('Wall time includes parser read/parse/dedup/export/validation; excludes setup. '
                                      'RSS is current process only; sampled peak can miss spikes. '
                                      'Storage includes shards, checkpoint and audit in parsed/. '
                                      'Null unresolved/conflict metrics are unavailable, not zero.')
        with (output / 'resources.csv').open('w', newline='', encoding='utf8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['elapsed_seconds', 'rss_bytes'])
            writer.writerows(samples)
        (output / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--persistent-root', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--sample-seconds', type=float, default=0.25)
    parser.add_argument('--include-partial', action='store_true')
    parser.add_argument('--incomplete-group-data', action='store_true',
                        help='label a run with missing member data; blocks official group acceptance')
    args = parser.parse_args()
    benchmark(args.persistent_root, args.out, limit=args.limit,
              sample_seconds=args.sample_seconds, include_partial=args.include_partial,
              group_data_complete=False if args.incomplete_group_data else None)


if __name__ == '__main__':
    main()
