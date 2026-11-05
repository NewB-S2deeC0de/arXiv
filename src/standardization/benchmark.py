"""Reproducible CPU-only B2 microbenchmark with isolated stage/case workers.

Timing excludes file I/O, workload generation, validation and JSON writing.
Memory is separately traced Python allocations, NOT process RSS.
"""
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc

from .bibliography import extract_bibliography
from .reference_links import extract_reference_links


STAGES = ('bibliography', 'links', 'pipeline')


def make_workload(raw, multiplier):
    """Repeat reviewed entry bodies; unique synthetic keys, one original preamble.

    multiplier=1 returns original input. Not independent publications/versions.
    Requires one structurally safe bibliography and all keys distinct/valid.
    """
    if type(multiplier) is not int or multiplier < 1:
        raise ValueError('multiplier must be a positive integer')
    parsed = extract_bibliography(raw)
    keys = [e.citation_key for e in parsed.entries]
    if (len(parsed.regions) != 1 or not parsed.entries or parsed.warnings
            or not parsed.regions[0].safe_to_exclude or None in keys or len(set(keys)) != len(keys)):
        raise ValueError('Benchmark input needs one safe, nonempty bibliography with unique valid keys and no warnings')
    if multiplier == 1:
        return raw
    region = parsed.regions[0]
    parts = [raw[:parsed.entries[0].start]]
    for copy in range(multiplier):
        for index, entry in enumerate(parsed.entries):
            # Synthetic header intentionally simple; original body untouched.
            parts.append('\\bibitem{bench_' + str(copy) + '_' + str(index) + '}')
            parts.append(entry.raw_content)
    parts.append(raw[region.body_end:])
    return ''.join(parts)


def run_worker(path, multiplier, stage, repeats, warmups):
    if stage not in STAGES or repeats < 1 or warmups < 0:
        raise ValueError('Invalid stage/repeats/warmups')
    source_bytes = Path(path).read_bytes()
    source_raw = source_bytes.decode('utf-8')
    baseline = extract_bibliography(source_raw)
    raw = make_workload(source_raw, multiplier)
    prepared = extract_bibliography(raw)
    expected_entries = len(baseline.entries) * multiplier
    baseline_links = tuple(extract_reference_links(e.raw_content) for e in baseline.entries)
    expected_candidates = sum(len(x.candidates) for x in baseline_links) * multiplier

    def links(entries):
        return tuple(extract_reference_links(e.raw_content) for e in entries)

    def operation():
        if stage == 'bibliography':
            return extract_bibliography(raw)
        if stage == 'links':
            return links(prepared.entries)
        result = extract_bibliography(raw)
        return result, links(result.entries)

    def validate(result):
        bib = result if stage == 'bibliography' else result[0] if stage == 'pipeline' else None
        evidence = result if stage == 'links' else result[1] if stage == 'pipeline' else None
        if bib is not None:
            if len(bib.entries) != expected_entries or bib.warnings or bib.raw != raw:
                raise RuntimeError('Bibliography output validation failed')
        if evidence is not None:
            if len(evidence) != expected_entries or sum(len(x.candidates) for x in evidence) != expected_candidates:
                raise RuntimeError('Link output validation failed')
            # Check warning count against baseline rather than assume no warnings.
            if sum(len(x.warnings) for x in evidence) != sum(len(x.warnings) for x in baseline_links) * multiplier:
                raise RuntimeError('Link diagnostics validation failed')

    for _ in range(warmups):
        result = operation()
        validate(result)
        del result
    samples = []
    for _ in range(repeats):
        gc.collect()  # Outside timing; normal cyclic GC remains enabled during operation.
        start = time.perf_counter_ns()
        result = operation()
        elapsed = time.perf_counter_ns() - start
        samples.append(elapsed / 1_000_000)
        validate(result)
        del result
    gc.collect()
    tracemalloc.start()
    try:
        result = operation()
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    validate(result)
    return {'multiplier': multiplier, 'workload': 'real_original' if multiplier == 1 else 'synthetic_repeated_entries',
            'stage': stage, 'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
            'workload_sha256_utf8': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'input_characters': len(raw), 'input_utf8_bytes': len(raw.encode('utf-8')),
            'entries': expected_entries, 'expected_link_candidates': expected_candidates,
            'repeats': repeats, 'warmups': warmups, 'samples_ms': samples,
            'median_ms': statistics.median(samples), 'min_ms': min(samples), 'max_ms': max(samples),
            'traced_python_peak_bytes': peak, 'traced_python_retained_bytes': current,
            'validation': 'passed'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='UTF-8 .tex with one safe bibliography')
    parser.add_argument('--output', type=Path, help='New .json report, never overwrite')
    parser.add_argument('--multipliers', nargs='+', type=int, default=[1, 10, 100])
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--worker', choices=STAGES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.warmups < 0 or any(m < 1 for m in args.multipliers):
        parser.error('Positive repeats/multipliers and nonnegative warmups required')
    if args.worker:
        print(json.dumps(run_worker(args.source, args.multipliers[0], args.worker, args.repeats, args.warmups)))
        return 0
    if not args.output or args.output.suffix.lower() != '.json' or args.output.exists():
        parser.error('--output must name a new .json file')
    worker_env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    inherited = worker_env.get('PYTHONPATH')
    worker_env['PYTHONPATH'] = os.pathsep.join([source_root, inherited] if inherited else [source_root])
    rows = []
    for multiplier in sorted(set(args.multipliers)):
        for stage in STAGES:
            command = [sys.executable, '-B', '-m', 'standardization.benchmark', str(args.source.resolve()),
                       '--worker', stage, '--multipliers', str(multiplier),
                       '--repeats', str(args.repeats), '--warmups', str(args.warmups)]
            completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                       check=True, timeout=600, env=worker_env)
            row = json.loads(completed.stdout)
            rows.append(row)
            print(f'{stage}: x{multiplier}, {row["entries"]} entries, median {row["median_ms"]:.3f} ms', flush=True)
    report = {'benchmark_format_version': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'environment': {'python': sys.version, 'executable': sys.executable,
                              'platform': platform.platform(), 'machine': platform.machine(),
                              'processor_reported': platform.processor(), 'logical_cpu_count': os.cpu_count()},
              'method': {'isolated_worker_per_stage_case': True, 'single_python_process_per_operation': True,
                         'time_clock': 'perf_counter_ns', 'memory': 'Separate tracemalloc pass; Python allocations only, not RSS',
                         'excluded': 'I/O, input/prepared entry allocation, imports, process startup, validation, JSON output',
                         'gc': 'collect before each sample, outside timing; enabled during operation',
                         'scope': 'B2 microbenchmark, not parser M2 or Colab performance'},
              'source_file': str(args.source.resolve()), 'results': rows}
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
