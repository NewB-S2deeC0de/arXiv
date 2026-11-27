import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from crawler.export import PAPER_SCHEMA, REFERENCE_SCHEMA
from matching.metrics import evaluate_predictions, reciprocal_rank_at_5
from matching.upload_hf import DEFAULT_CARD, SCHEMAS, stage_dataset
from matching.validate_predictions import (
    CANDIDATE_COLUMNS,
    PREDICTIONS_SCHEMA,
    build_predictions,
    validate_predictions,
)
from parser.contract import BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA


def write_rows(path, rows, schema=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


@pytest.fixture
def inputs(tmp_path):
    m1, m2 = tmp_path / "export", tmp_path / "parsed"
    papers = [
        {"arxiv_id": "2410.00001", "partition": "train"},
        {"arxiv_id": "2410.00002", "partition": "valid"},
        {"arxiv_id": "2410.00003", "partition": "test"},
    ]
    train, valid, test = [row["arxiv_id"] for row in papers]
    entries = [
        {"arxiv_id": paper, "citation_key": key, "entry_type": "article"}
        for paper, key in [(train, "shared"), (valid, "shared"),
                           (test, "shared"), (test, "unmatched")]
    ]
    targets = ["1706.00001", "1706.00002", "1706.00003", "1706.00004"]
    labelled = [
        {**entry, "groundtruth_arxiv_id": target}
        for entry, target in zip(entries, [*targets[:3], None])
    ]
    rankings = [
        {**entry, **dict(zip(CANDIDATE_COLUMNS, candidates))}
        for entry, candidates in zip(entries, [
            [targets[0], None, None, None, None],
            [targets[1], None, None, None, None],
            [targets[3], targets[2], None, None, None],
            [None] * 5,
        ])
    ]
    write_rows(m1 / "papers.parquet", papers + [{"arxiv_id": "2410.99999"}], PAPER_SCHEMA)
    write_rows(m2 / "elements.parquet", [
        {"element_id": "id", "arxiv_id": train, "kind": "document"},
    ], ELEMENTS_SCHEMA)
    write_rows(m2 / "hierarchy.parquet", [], HIERARCHY_SCHEMA)
    return {
        "m1": m1,
        "m2": m2,
        "bibtex": write_rows(m2 / "bibtex.parquet", entries, BIBTEX_SCHEMA),
        "references": write_rows(m1 / "references.parquet", [
            {"arxiv_id": paper, "ref_arxiv_id": target}
            for paper, target in zip([train, valid, test, test], targets)
        ], REFERENCE_SCHEMA),
        "splits": write_rows(tmp_path / "splits.parquet", papers),
        "labels": write_rows(tmp_path / "labels.parquet", labelled),
        "rankings": write_rows(tmp_path / "ranked_candidates.parquet", rankings),
        "output": tmp_path / "predictions.parquet",
    }


def build(files):
    return build_predictions(*(files[key] for key in (
        "output", "splits", "bibtex", "references", "labels", "rankings",
    )))


@pytest.mark.parametrize(("target", "candidates", "score"), [
    ("a", ["a"], 1.0),
    ("a", [None, "a"], 0.5),
    ("a", ["b", None, None, None, "a"], 0.2),
    ("a", [None] * 5 + ["a"], 0.0),
    (None, [None] * 5, 0.0),
])
def test_reciprocal_rank(target, candidates, score):
    assert reciprocal_rank_at_5(target, candidates) == score


def test_build_evaluate_and_stage(inputs, tmp_path):
    assert build(inputs) == 4
    assert pq.read_schema(inputs["output"]).equals(PREDICTIONS_SCHEMA)
    assert evaluate_predictions(inputs["output"], inputs["labels"]) == {
        "valid": {"n_queries": 1, "mrr_at_5": 1.0},
        "test": {"n_queries": 2, "mrr_at_5": 0.25},
    }
    destination = tmp_path / "dataset"
    stage_dataset(destination, *(inputs[key] for key in (
        "m1", "m2", "output", "splits", "labels",
    )), DEFAULT_CARD, shard_rows=2)
    card = (destination / "README.md").read_text(encoding="utf-8")
    configs = yaml.safe_load(card.split("---", 2)[1])["configs"]
    assert {config["config_name"] for config in configs} == SCHEMAS.keys()
    for config in configs:
        name = config["config_name"]
        pattern = config["data_files"][0]["path"]
        shards = sorted(destination.glob(pattern))
        assert shards
        for shard in shards:
            assert pq.read_schema(shard).equals(SCHEMAS[name])
            assert pq.ParquetFile(shard).metadata.num_rows <= 2
    papers = pq.read_table(destination / "data" / "papers")
    assert papers.num_rows == 4
    assert "venue_source" not in papers.column_names


@pytest.mark.parametrize(("change", "error"), [
    ({"arxiv_id": "2410.99999"}, "unexpected keys"),
    ({"partition": "test"}, "wrong partition"),
    ({"partition": "other"}, "wrong partition"),
    ({"cand_1": "1706.00002"}, "outside its reference pool"),
    ({"groundtruth_arxiv_id": None}, "ground truth different from labels"),
])
def test_reject_invalid_predictions(inputs, change, error):
    build(inputs)
    rows = pq.read_table(inputs["output"]).to_pylist()
    rows[0].update(change)
    write_rows(inputs["output"], rows, PREDICTIONS_SCHEMA)
    with pytest.raises(ValueError, match=error):
        validate_predictions(*(inputs[key] for key in (
            "output", "splits", "bibtex", "references", "labels",
        )))


def test_missing_label_does_not_count_as_null_groundtruth(inputs):
    build(inputs)
    labels = pq.read_table(inputs["labels"])
    pq.write_table(labels.slice(0, labels.num_rows - 1), inputs["labels"])
    with pytest.raises(ValueError, match="missing keys"):
        evaluate_predictions(inputs["output"], inputs["labels"])
