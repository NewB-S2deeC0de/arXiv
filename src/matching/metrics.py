

"""Calculate entry-weighted MRR@5 for validation and test."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow.parquet as pq

from .validate_predictions import (
    CANDIDATE_COLUMNS,
    ENTRY_KEY,
    GROUNDTRUTH,
    PREDICTIONS_SCHEMA,
    _index,
    _require_keys,
    read_labels,
)


def reciprocal_rank_at_5(groundtruth, candidates):
    if groundtruth is None:
        return 0.0
    for rank, candidate in enumerate(candidates[:5], start=1):
        if candidate == groundtruth:
            return 1.0 / rank
    return 0.0


def evaluate_predictions(predictions, labels):
    table = pq.read_table(predictions)
    if not table.schema.equals(PREDICTIONS_SCHEMA):
        raise ValueError("predictions: expected the final M3 schema")
    rows = _index(table.to_pylist(), ENTRY_KEY, "predictions")
    labelled = read_labels(labels)
    _require_keys(labelled.keys(), rows.keys(), "labels")
    totals = {"valid": 0.0, "test": 0.0}
    counts = dict.fromkeys(totals, 0)
    for key, row in rows.items():
        target = labelled[key][GROUNDTRUTH]
        if row[GROUNDTRUTH] != target:
            raise ValueError(f"predictions: {key!r} has ground truth different from labels")
        partition = row["partition"]
        if partition == "train":
            continue
        if partition not in counts:
            raise ValueError(f"predictions: {key!r} has invalid partition {partition!r}")
        candidates = [row[column] for column in CANDIDATE_COLUMNS]
        counts[partition] += 1
        totals[partition] += reciprocal_rank_at_5(target, candidates)
    for partition, count in counts.items():
        if count == 0:
            raise ValueError(f"predictions: no labelled queries in {partition!r}")
    return {
        partition: {"n_queries": count, "mrr_at_5": totals[partition] / count}
        for partition, count in counts.items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("runtime/matching/mrr_report.json"))
    args = parser.parse_args()
    try:
        report = evaluate_predictions(args.predictions, args.labels)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=args.out.parent) as directory:
            staged = Path(directory) / "mrr_report.json"
            staged.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            staged.replace(args.out)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(args.out)


if __name__ == "__main__":
    main()
