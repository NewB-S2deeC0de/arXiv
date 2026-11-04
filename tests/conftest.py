import json
from pathlib import Path

import pytest

from crawler.storage import digest


def write_source(root, arxiv_id: str, version: int, files: dict[str, str],
                 status: str = 'succeeded', *, corrupt_inventory: bool = False) -> Path:
    source = Path(root) / 'data' / arxiv_id / 'versions' / f'v{version}' / 'source'
    source.mkdir(parents=True, exist_ok=True)
    tex = source / 'tex'
    for name, content in files.items():
        target = tex / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf8')
    inventory = dict(
        tex_count=sum(1 for name in files if name.endswith('.tex')),
        bib_count=sum(1 for name in files if name.endswith('.bib')),
        compressed_bytes=0, unpacked_bytes=0, kept_bytes=0, discarded_files=0,
    )
    inventory_path = source / 'inventory.json'
    inventory_path.write_text(json.dumps(inventory), encoding='utf8')
    checksums = {'inventory.json': digest(inventory_path)}
    if corrupt_inventory:
        inventory_path.write_text(json.dumps(dict(inventory, tex_count=999)), encoding='utf8')
    marker = dict(schema_version=1, key=[arxiv_id, version, 'source', 0],
                  status=status, checksums=checksums, captured_at='2024-01-01T00:00:00+00:00',
                  run_id='test', bytes=0)
    (source / 'complete.json').write_text(json.dumps(marker), encoding='utf8')
    return source


@pytest.fixture
def make_source():
    return write_source
