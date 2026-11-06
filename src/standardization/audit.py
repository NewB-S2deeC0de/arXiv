"""Offline file-level B2 audit. Does not gather TeX or define final C records.

Run: python -m standardization.audit INPUT [INPUT ...] --output report.json
"""
import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys

from .bibliography import extract_bibliography
from .models import SourceContext
from .reference_links import extract_reference_links


def audit_file(path, *, encoding='utf-8', max_bytes=16 * 1024 * 1024):
    """Read exact bytes with a limit; decoding is strict, never errors=replace."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('max_bytes must be a positive integer')
    path = Path(path).resolve()
    row = {'source_file': str(path), 'encoding': encoding}
    if not path.is_file():
        return dict(row, status='read_error', error='not_a_file', message='Input is not a readable source file.')
    if path.suffix.lower() == '.bib':
        return dict(row, status='external_bib_input',
                    message='BibTeX fields are not parsed by this audit.')
    if path.suffix.lower() not in {'.tex', '.bbl'}:
        return dict(row, status='unsupported_input', message='Expected .tex, .bbl or .bib.')
    try:
        with path.open('rb') as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            return dict(row, status='read_error', error='file_size_limit',
                        message=f'Input exceeds {max_bytes} bytes; not parsed.')
        raw = data.decode(encoding, errors='strict')
    except (OSError, UnicodeError, LookupError) as exc:
        return dict(row, status='read_error', error=type(exc).__name__, message=str(exc))
    starts = [0] + [m.end() for m in re.finditer(r'\r\n|\r|\n', raw)]

    def location(start, end):
        first = bisect_right(starts, start) - 1
        last = bisect_right(starts, end) - 1
        return {'start': start, 'end': end,
                'start_line': first + 1, 'start_column': start - starts[first] + 1,
                'end_line': last + 1, 'end_column': end - starts[last] + 1}

    result = extract_bibliography(raw, source=SourceContext(source_file=str(path)))
    entries = []
    for index, entry in enumerate(result.entries):
        links = extract_reference_links(entry.raw_content)
        evidence = []
        for candidate in links.candidates:
            evidence.append(dict(asdict(candidate),
                file_location=location(entry.content_start + candidate.start,
                                       entry.content_start + candidate.end)))
        link_warnings = [dict(asdict(w),
            file_location=location(entry.content_start + w.start, entry.content_start + w.end))
            for w in links.warnings]
        entries.append({'entry_index': index, 'bibitem': asdict(entry),
                        'file_location': location(entry.start, entry.end),
                        'link_candidates': evidence, 'link_warnings': link_warnings})
    warnings = [dict(asdict(w), file_location=location(w.start, w.end)) for w in result.warnings]
    has_warnings = bool(warnings) or any(e['link_warnings'] for e in entries)
    status = 'needs_review' if has_warnings else 'extracted' if result.regions else 'no_literal_bibliography'
    return dict(row, status=status, bytes=len(data), characters=len(raw),
                sha256=hashlib.sha256(data).hexdigest(),
                entry_count=len(entries), region_count=len(result.regions),
                exclusion_spans=result.exclusion_spans,
                regions=[asdict(r) for r in result.regions],
                unparsed=[asdict(f) for f in result.unparsed],
                warnings=warnings, entries=entries)


def audit_paths(paths, *, encoding='utf-8', max_bytes=16 * 1024 * 1024):
    """Inspect files independently. No root selection or input/include resolution."""
    found = set()
    failures = []
    for value in paths:
        path = Path(value).resolve()
        if not path.exists():
            failures.append({'source_file': str(path), 'status': 'read_error',
                             'error': 'missing_input', 'message': 'Input does not exist.'})
        elif path.is_dir():
            try:
                found.update(p.resolve() for p in path.rglob('*')
                             if p.is_file() and p.suffix.lower() in {'.tex', '.bbl', '.bib'})
            except OSError as exc:
                failures.append({'source_file': str(path), 'status': 'read_error',
                                 'error': type(exc).__name__, 'message': str(exc)})
        else:
            found.add(path)
    rows = [audit_file(p, encoding=encoding, max_bytes=max_bytes)
            for p in sorted(found, key=str)] + failures
    rows.sort(key=lambda r: r['source_file'])
    counts = Counter(r['status'] for r in rows)
    candidates = [c for r in rows for e in r.get('entries', []) for c in e['link_candidates']]
    return {'audit_format_version': 1,
            'scope': 'Independent file-level B2 audit; not final M2 bibtex records or dataset completeness.',
            'offsets': 'Unicode characters, zero-based [start,end); line/column one-based; no newline conversion.',
            'candidate_offsets': 'start/end are entry raw_content offsets; file_location is source-file relative.',
            'summary': {'files': len(rows), 'statuses': dict(sorted(counts.items())),
                        'entries': sum(r.get('entry_count', 0) for r in rows),
                        'url_candidates': sum(c['kind'] == 'url' for c in candidates),
                        'doi_candidates': sum(c['kind'] == 'doi' for c in candidates)},
            'files': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', help='Source files or directories, recursively inspected')
    parser.add_argument('--output', type=Path, help='New .json output; existing files are never overwritten')
    parser.add_argument('--encoding', default='utf-8', help='Explicit Python codec, strict decoding')
    parser.add_argument('--max-bytes', type=int, default=16 * 1024 * 1024, help='Maximum bytes per LaTeX file')
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error('--max-bytes must be positive')
    if args.output and args.output.suffix.lower() != '.json':
        parser.error('--output must end in .json')
    report = audit_paths(args.inputs, encoding=args.encoding, max_bytes=args.max_bytes)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        try:
            with args.output.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(payload)
        except OSError as exc:
            print(f'Cannot write audit: {exc}', file=sys.stderr)
            return 2
        print(json.dumps(report['summary'], ensure_ascii=False))
    else:
        print(payload, end='')
    return 2 if report['summary']['statuses'].get('read_error') or report['summary']['statuses'].get('unsupported_input') else 0


if __name__ == '__main__':
    raise SystemExit(main())
