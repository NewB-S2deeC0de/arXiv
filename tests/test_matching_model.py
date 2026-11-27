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
    assert first['outputs']['predictions_raw'].split('\\')[-1] == 'predictions_raw.parquet'
    assert first['pairs_total'] == second['pairs_total']
    assert (
        pq.read_table(tmp_path / 'first' / 'predictions_raw.parquet').to_pylist()
        == pq.read_table(tmp_path / 'second' / 'predictions_raw.parquet').to_pylist()
    )
