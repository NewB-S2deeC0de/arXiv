"""Copy M1 source records from multiple runtimes into one fresh benchmark input.

No PDFs/references/progress are needed by the parser. Input runtimes are read-only.
Run only after all crawlers have stopped and Drive has finished synchronizing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import quote

from .adapter import publications_with_records, list_publications


def _digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def _files(directory):
    if directory.is_symlink():
        raise ValueError(f'snapshot does not accept source symlinks: {directory}')
    if not directory.exists():
        return None
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'snapshot does not accept source symlinks: {path}')
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = _digest(path)
    return result


def prepare_snapshot(roots, output):
    roots = [Path(root).resolve() for root in roots]
    output = Path(output).resolve()
    if not roots or len(set(roots)) != len(roots):
        raise ValueError('provide distinct M1 runtime roots')
    for root in roots:
        if not (root / 'data').is_dir():
            raise ValueError(f'M1 data directory not found: {root}')
        if output == root or root in output.parents or output in root.parents:
            raise ValueError('snapshot output must be separate from every input runtime')
    if output.exists():
        raise ValueError('snapshot requires a new output directory')

    records = {}
    inventories = {}
    members = []
    for root in roots:
        publications = publications_with_records(root)
        paper_dirs = dict(list_publications(root))
        inventories[root] = {arxiv_id: [r.version for r in versions]
                             for arxiv_id, versions in publications}
        if not publications:
            raise ValueError(f'no publications in member runtime: {root}')
        members.append(dict(root=str(root), publications=len(publications)))
        for arxiv_id, versions in publications:
            for record in versions:
                source = paper_dirs[arxiv_id] / 'versions' / f'v{record.version}' / 'source'
                files = _files(source)
                key = (arxiv_id, record.version)
                if key in records:
                    if records[key]['files'] != files:
                        raise ValueError(f'conflicting duplicate version: {key}')
                    records[key]['owners'].append(str(root))
                else:
                    records[key] = dict(source=source, files=files, owners=[str(root)])

    output.mkdir(parents=True)
    manifest = dict(schema_version=1, status='preparing', members=members, versions=[])
    manifest_path = output / 'snapshot.json'
    try:
        # Preserve publications with no version records as attempted publications.
        for root in roots:
            for arxiv_id, _ in publications_with_records(root):
                (output / 'data' / quote(arxiv_id, safe='') / 'versions').mkdir(parents=True, exist_ok=True)
        for (arxiv_id, version), item in sorted(records.items()):
            target = output / 'data' / quote(arxiv_id, safe='') / 'versions' / f'v{version}'
            target.mkdir(parents=True)
            if item['files'] is not None:
                shutil.copytree(item['source'], target / 'source')
            if _files(target / 'source') != item['files']:
                raise ValueError(f'snapshot copy verification failed: {arxiv_id} v{version}')
            manifest['versions'].append(dict(arxiv_id=arxiv_id, version=version,
                                             owners=item['owners'], files=item['files']))
        # Detect source mutations during copying, including duplicate owners.
        for root in roots:
            current = publications_with_records(root)
            if {a: [r.version for r in versions] for a, versions in current} != inventories[root]:
                raise ValueError('source publication/version inventory changed during snapshot')
            paper_dirs = dict(list_publications(root))
            for arxiv_id, versions in current:
                for record in versions:
                    item = records[(arxiv_id, record.version)]
                    source = paper_dirs[arxiv_id] / 'versions' / f'v{record.version}' / 'source'
                    if _files(source) != item['files']:
                        raise ValueError('source files changed during snapshot; stop crawlers first')
        manifest['status'] = 'completed'
        payload = json.dumps(manifest['versions'], sort_keys=True).encode('utf8')
        manifest['fingerprint'] = hashlib.sha256(payload).hexdigest()
        return manifest
    except Exception as exc:
        manifest.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    prepare_snapshot(args.root, args.out)


if __name__ == '__main__':
    main()
