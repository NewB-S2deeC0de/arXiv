"""Build and validate the final M3 predictions table."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq


PARTITIONS = {"train", "valid", "test"}
ENTRY_KEY = ("arxiv_id", "citation_key")
CANDIDATE_COLUMNS = tuple(f"cand_{rank}" for rank in range(1, 6))
GROUNDTRUTH = "groundtruth_arxiv_id"
PREDICTIONS_SCHEMA = pa.schema([
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("partition", pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("citation_key", pa.string(), nullable=False),
    pa.field(GROUNDTRUTH, pa.string()),
    *(pa.field(name, pa.string()) for name in CANDIDATE_COLUMNS),
])


def _index(rows, columns, name):
    indexed = {}
    for row in rows:
        key = tuple(row[column] for column in columns)
        if any(not isinstance(value, str) or not value.strip() for value in key):
            raise ValueError(f"{name}: invalid key {key!r}")
        if key in indexed:
            raise ValueError(f"{name}: duplicate key {key!r}")
        indexed[key] = row
    return indexed


def _require_keys(actual, expected, name):
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ValueError(
            f"{name}: {len(missing)} missing keys {sorted(missing)[:3]!r}, "
            f"{len(extra)} unexpected keys {sorted(extra)[:3]!r}"
        )


def read_scope(splits, bibtex):
    """Return the authoritative partition map and expected BibTeX keys."""
    split_rows = pq.read_table(splits, columns=["arxiv_id", "partition"]).to_pylist()
    partitions = {
        paper_id: row["partition"]
        for (paper_id,), row in _index(split_rows, ("arxiv_id",), "splits").items()
    }
    if not partitions:
        raise ValueError("splits: no publications")
    for paper_id, partition in partitions.items():
        if not isinstance(partition, str) or partition not in PARTITIONS:
            raise ValueError(f"splits: {paper_id!r} has invalid partition {partition!r}")

    bibtex_rows = pq.read_table(
        bibtex, columns=list(ENTRY_KEY),
        filters=[("arxiv_id", "in", list(partitions))],
    ).to_pylist()
    entries = set(_index(bibtex_rows, ENTRY_KEY, "bibtex"))
    _require_keys({paper_id for paper_id, _ in entries}, partitions.keys(), "split publications")
    return partitions, entries


def read_labels(path):
    rows = pq.read_table(path, columns=[*ENTRY_KEY, GROUNDTRUTH]).to_pylist()
    return _index(rows, ENTRY_KEY, "labels")


def _validate_table(table, partitions, entries, references, labels=None):
    if not table.schema.equals(PREDICTIONS_SCHEMA):
        raise ValueError(f"predictions: expected schema\n{PREDICTIONS_SCHEMA}\ngot\n{table.schema}")
    predictions = _index(table.to_pylist(), ENTRY_KEY, "predictions")
    _require_keys(predictions.keys(), entries, "predictions")
    if labels is not None:
        _require_keys(labels.keys(), entries, "labels")
    reference_keys = {
        (row["arxiv_id"], row["ref_arxiv_id"])
        for row in pq.read_table(
            references, columns=["arxiv_id", "ref_arxiv_id"],
            filters=[("arxiv_id", "in", list(partitions))],
        ).to_pylist()
    }
    for key, row in predictions.items():
        if row["partition"] != partitions[key[0]]:
            raise ValueError(f"predictions: {key!r} has wrong partition {row['partition']!r}")
        if labels is not None and row[GROUNDTRUTH] != labels[key][GROUNDTRUTH]:
            raise ValueError(f"predictions: {key!r} has ground truth different from labels")
        candidates = [row[column] for column in CANDIDATE_COLUMNS if row[column] is not None]
        if len(candidates) != len(set(candidates)):
            raise ValueError(f"predictions: {key!r} contains duplicate candidates")
        for column in (GROUNDTRUTH, *CANDIDATE_COLUMNS):
            candidate = row[column]
            if candidate is not None and (key[0], candidate) not in reference_keys:
                raise ValueError(
                    f"predictions: {key!r} {column}={candidate!r} is outside its reference pool"
                )
    return table.num_rows


def validate_predictions(predictions, splits, bibtex, references, labels=None):
    partitions, entries = read_scope(splits, bibtex)
    return _validate_table(
        pq.read_table(predictions), partitions, entries, references,
        read_labels(labels) if labels is not None else None,
    )


def build_predictions(output, splits, bibtex, references, labels, rankings):
    partitions, entries = read_scope(splits, bibtex)
    labelled = read_labels(labels)
    ranked = _index(
        pq.read_table(rankings, columns=[*ENTRY_KEY, *CANDIDATE_COLUMNS]).to_pylist(),
        ENTRY_KEY, "rankings",
    )
    _require_keys(labelled.keys(), entries, "labels")
    _require_keys(ranked.keys(), entries, "rankings")
    rows = [
        {
            "arxiv_id": key[0],
            "partition": partitions[key[0]],
            "citation_key": key[1],
            GROUNDTRUTH: labelled[key][GROUNDTRUTH],
            **{column: ranked[key][column] for column in CANDIDATE_COLUMNS},
        }
        for key in sorted(entries)
    ]
    table = pa.Table.from_pylist(rows, schema=PREDICTIONS_SCHEMA)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as directory:
        staged = Path(directory) / "predictions.parquet"
        pq.write_table(table, staged)
        count = _validate_table(pq.read_table(staged), partitions, entries, references, labelled)
        staged.replace(output)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--predictions", type=Path, help="Validate an existing table")
    mode.add_argument("--output", type=Path, help="Build and validate the table at this path")
    parser.add_argument("--splits", type=Path, required=True)
    parser.add_argument("--bibtex", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--rankings", type=Path)
    args = parser.parse_args()
    sources = (args.splits, args.bibtex, args.references)
    try:
        if args.output:
            if args.labels is None or args.rankings is None:
                parser.error("--output requires --labels and --rankings")
            count = build_predictions(args.output, *sources, args.labels, args.rankings)
        else:
            if args.rankings is not None:
                parser.error("--rankings is only used with --output")
            count = validate_predictions(args.predictions, *sources, args.labels)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Validated {count:,} prediction rows")


if __name__ == "__main__":
    main()
