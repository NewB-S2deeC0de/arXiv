from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

from matching.features import FEATURE_NAMES
from matching.model import PREDICTION_COLUMNS, run

BIBTEX_SCHEMA = pa.schema(
    [
        ('arxiv_id', pa.string()),
        ('citation_key', pa.string()),
        ('title', pa.string()),
        ('authors', pa.list_(pa.string())),
        ('year', pa.int64()),
        ('doi', pa.string()),
    ]
)
REFERENCES_SCHEMA = pa.schema(
    [
        ('arxiv_id', pa.string()),
        ('ref_arxiv_id', pa.string()),
        ('ref_title', pa.string()),
        ('ref_authors', pa.list_(pa.string())),
        ('ref_submission_date', pa.string()),
        ('ref_doi', pa.string()),
    ]
)
ELEMENTS_SCHEMA = pa.schema(
    [
        ('element_id', pa.string()),
        ('arxiv_id', pa.string()),
        ('kind', pa.string()),
        ('content', pa.string()),
    ]
)
GROUNDTRUTH_SCHEMA = pa.schema(
    [
        ('arxiv_id', pa.string()),
        ('citation_key', pa.string()),
        ('groundtruth_arxiv_id', pa.string()),
        ('partition', pa.string()),
    ]
)


def _write(path, rows, schema):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), str(path))


def _fixture(tmp_path):
    bibtex = [
        {'arxiv_id': 'p1', 'citation_key': 'a1', 'title': 'Deep Learning', 'authors': ['Smith'], 'year': 2020, 'doi': '10.1/A'},
        {'arxiv_id': 'p1', 'citation_key': 'a2', 'title': 'Graph Models', 'authors': ['Lee'], 'year': 2019, 'doi': None},
        {'arxiv_id': 'p2', 'citation_key': 'b1', 'title': 'Language Models', 'authors': ['Brown'], 'year': 2021, 'doi': '10.2/B'},
        {'arxiv_id': 'p3', 'citation_key': 'c1', 'title': 'Robotics', 'authors': ['Nguyen'], 'year': 2018, 'doi': None},
    ]
    references = [
        {'arxiv_id': 'p1', 'ref_arxiv_id': 'r1', 'ref_title': 'Deep Learning', 'ref_authors': ['Smith'], 'ref_submission_date': '2020-01-01', 'ref_doi': '10.1/a'},
        {'arxiv_id': 'p1', 'ref_arxiv_id': 'r2', 'ref_title': 'Graph Models', 'ref_authors': ['Lee'], 'ref_submission_date': '2019-02-01', 'ref_doi': None},
        {'arxiv_id': 'p1', 'ref_arxiv_id': 'r3', 'ref_title': 'Unrelated Paper', 'ref_authors': ['Other'], 'ref_submission_date': '2015-01-01', 'ref_doi': None},
        {'arxiv_id': 'p2', 'ref_arxiv_id': 's1', 'ref_title': 'Language Models', 'ref_authors': ['Brown'], 'ref_submission_date': '2021-03-01', 'ref_doi': '10.2/b'},
        {'arxiv_id': 'p3', 'ref_arxiv_id': 't1', 'ref_title': 'Robotics', 'ref_authors': ['Nguyen'], 'ref_submission_date': '2018-05-01', 'ref_doi': None},
        {'arxiv_id': 'p3', 'ref_arxiv_id': 't2', 'ref_title': 'Vision', 'ref_authors': ['Tan'], 'ref_submission_date': '2017-05-01', 'ref_doi': None},
    ]
    elements = [
        {'element_id': 'p1s1', 'arxiv_id': 'p1', 'kind': 'sentence', 'content': 'We cite a1 here.'},
        {'element_id': 'p2s1', 'arxiv_id': 'p2', 'kind': 'sentence', 'content': 'No citation markers.'},
    ]
    groundtruth = [
        {'arxiv_id': 'p1', 'citation_key': 'a1', 'groundtruth_arxiv_id': 'r1', 'partition': 'train'},
        {'arxiv_id': 'p1', 'citation_key': 'a2', 'groundtruth_arxiv_id': 'r2', 'partition': 'train'},
        {'arxiv_id': 'p2', 'citation_key': 'b1', 'groundtruth_arxiv_id': 's1', 'partition': 'valid'},
        {'arxiv_id': 'p3', 'citation_key': 'c1', 'groundtruth_arxiv_id': 't1', 'partition': 'test'},
    ]
    paths = {
        'bibtex': tmp_path / 'bibtex.parquet',
        'references': tmp_path / 'references.parquet',
        'elements': tmp_path / 'elements.parquet',
        'groundtruth': tmp_path / 'groundtruth.parquet',
    }
    _write(paths['bibtex'], bibtex, BIBTEX_SCHEMA)
    _write(paths['references'], references, REFERENCES_SCHEMA)
    _write(paths['elements'], elements, ELEMENTS_SCHEMA)
    _write(paths['groundtruth'], groundtruth, GROUNDTRUTH_SCHEMA)
    return paths


def test_run_trains_lightgbm_and_writes_all_outputs(tmp_path):
    paths = _fixture(tmp_path)
    output_dir = tmp_path / 'matching'
    summary = run(
        bibtex_path=paths['bibtex'],
        references_path=paths['references'],
        elements_path=paths['elements'],
        groundtruth_path=paths['groundtruth'],
        output_dir=output_dir,
    )

    assert summary['pairs_total'] == 9
    assert summary['training_pairs'] == 6
    assert summary['positive_training_pairs'] == 2
    assert summary['predictions'] == 4
    assert summary['partitions'] == {'train': 2, 'valid': 1, 'test': 1}

    predictions = pq.read_table(output_dir / 'predictions_raw.parquet')
    assert predictions.schema.names == list(PREDICTION_COLUMNS)
    rows = predictions.to_pylist()
    by_key = {row['citation_key']: row for row in rows}
    assert by_key['a1']['groundtruth_arxiv_id'] == 'r1'
    assert by_key['a1']['partition'] == 'train'
    assert by_key['c1']['cand_1'] in {'t1', 't2'}
    for row in rows:
        candidates = [row[f'cand_{index}'] for index in range(1, 6)]
        assert len(set(candidate for candidate in candidates if candidate)) <= 5
        assert any(candidate is not None for candidate in candidates)

    scores = pq.read_table(output_dir / 'candidate_scores.parquet').to_pylist()
    assert len(scores) == 9
    assert all(feature in scores[0] for feature in FEATURE_NAMES)
    assert all(0.0 <= score['score'] <= 1.0 for score in scores)

    assert (output_dir / 'lgbm_model.txt').stat().st_size > 0
    importance_rows = (output_dir / 'feature_importances.csv').read_text(encoding='utf-8')
    assert all(feature in importance_rows for feature in FEATURE_NAMES)


def test_run_is_deterministic(tmp_path):
    paths = _fixture(tmp_path)
    first = run(
        bibtex_path=paths['bibtex'],
        references_path=paths['references'],
        elements_path=paths['elements'],
        groundtruth_path=paths['groundtruth'],
        output_dir=tmp_path / 'first',
    )
    second = run(
        bibtex_path=paths['bibtex'],
        references_path=paths['references'],
        elements_path=paths['elements'],
        groundtruth_path=paths['groundtruth'],
        output_dir=tmp_path / 'second',
    )
    assert Path(first['outputs']['predictions_raw']).name == 'predictions_raw.parquet'
    assert first['pairs_total'] == second['pairs_total']
    assert (
        pq.read_table(tmp_path / 'first' / 'predictions_raw.parquet').to_pylist()
        == pq.read_table(tmp_path / 'second' / 'predictions_raw.parquet').to_pylist()
    )


def test_discover_reference_path_ignores_hidden_and_staging_and_requires_manifest(tmp_path):
    import hashlib, json, os, time, pytest
    from matching.model import discover_reference_path

    root = tmp_path / 'dataset'
    root.mkdir()

    # 1. Valid export run-1 (older)
    run1 = root / 'exports' / 'run-1'
    run1.mkdir(parents=True)
    ref1 = run1 / 'references.parquet'
    ref1.write_text('parquet 1')
    checksum = hashlib.sha256(ref1.read_bytes()).hexdigest()
    (run1 / 'manifest.json').write_text(json.dumps({
        'schema_version': 3, 'run_id': 'run-1',
        'checksums': {'references.parquet': checksum},
    }))
    os.utime(ref1, (1000, 1000))

    # 2. Newer export in hidden folder .staging_exports
    stg_hidden = root / '.staging_exports' / 'run-2'
    stg_hidden.mkdir(parents=True)
    ref_hidden = stg_hidden / 'references.parquet'
    ref_hidden.write_text('hidden parquet')
    (stg_hidden / 'manifest.json').write_text('{"run_id": "run-2"}')
    os.utime(ref_hidden, (2000, 2000))

    # 3. Newer export in staging directory name
    stg_dir = root / 'exports' / 'staging-run-3'
    stg_dir.mkdir(parents=True)
    ref_stg = stg_dir / 'references.parquet'
    ref_stg.write_text('staging parquet')
    (stg_dir / 'manifest.json').write_text('{"run_id": "run-3"}')
    os.utime(ref_stg, (3000, 3000))

    # 4. Newer export with missing manifest
    no_manifest = root / 'exports' / 'run-4'
    no_manifest.mkdir(parents=True)
    ref_no_man = no_manifest / 'references.parquet'
    ref_no_man.write_text('no manifest')
    os.utime(ref_no_man, (4000, 4000))

    # 5. Newer export with corrupted manifest
    corrupt_man = root / 'exports' / 'run-5'
    corrupt_man.mkdir(parents=True)
    ref_corrupt = corrupt_man / 'references.parquet'
    ref_corrupt.write_text('corrupt manifest')
    (corrupt_man / 'manifest.json').write_text('not-valid-json {')
    os.utime(ref_corrupt, (5000, 5000))

    # Should select run1 because all newer ones are invalid/staging
    found = discover_reference_path(root)
    assert found == ref1

    # If run1 is deleted, should raise FileNotFoundError because no valid candidate remains
    ref1.unlink()
    with pytest.raises(FileNotFoundError, match='no valid references.parquet'):
        discover_reference_path(root)

