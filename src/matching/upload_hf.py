"""Stage the six final Parquet tables and upload them to Hugging Face."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from crawler.export import PAPER_SCHEMA, REFERENCE_SCHEMA
from parser.contract import BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA

from .validate_predictions import PREDICTIONS_SCHEMA, validate_predictions


DEFAULT_CARD = Path(__file__).resolve().parents[2] / "docs" / "huggingface" / "README.md"
PUBLISHED_PAPER_SCHEMA = pa.schema([
    field for field in PAPER_SCHEMA if field.name not in {"venue_source", "discovery_status"}
])


def _dictionary_field(schema, name):
    index = schema.get_field_index(name)
    field = schema.field(index).with_type(pa.dictionary(pa.int32(), pa.string()))
    return schema.set(index, field)


SCHEMAS = {
    "papers": PUBLISHED_PAPER_SCHEMA,
    "references": REFERENCE_SCHEMA,
    "elements": _dictionary_field(ELEMENTS_SCHEMA, "kind"),
    "hierarchy": HIERARCHY_SCHEMA,
    "bibtex": _dictionary_field(BIBTEX_SCHEMA, "entry_type"),
    "predictions": PREDICTIONS_SCHEMA,
}


def _write_shards(source, destination, schema, shard_rows):
    destination.mkdir(parents=True)
    parquet = pq.ParquetFile(source)
    for index, batch in enumerate(
        parquet.iter_batches(batch_size=shard_rows, columns=schema.names)
    ):
        table = pa.Table.from_batches([batch]).select(schema.names).cast(schema)
        pq.write_table(table, destination / f"part-{index:05d}.parquet")
    if not any(destination.iterdir()):
        pq.write_table(schema.empty_table(), destination / "part-00000.parquet")


def stage_dataset(destination, m1_export, m2_output, predictions, splits, labels, card,
                  shard_rows=50_000):
    if destination.exists():
        raise ValueError(f"Choose a new staging path: {destination} already exists")
    if shard_rows <= 0:
        raise ValueError("--shard-rows must be positive")
    sources = {
        "papers": m1_export / "papers.parquet",
        "references": m1_export / "references.parquet",
        **{name: m2_output / f"{name}.parquet" for name in ("elements", "hierarchy", "bibtex")},
        "predictions": predictions,
    }
    validate_predictions(predictions, splits, sources["bibtex"], sources["references"], labels)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as work:
        staged = Path(work) / "dataset"
        for name, schema in SCHEMAS.items():
            _write_shards(sources[name], staged / "data" / name, schema, shard_rows)
        (staged / "README.md").write_bytes(card.read_bytes())
        _check_package(staged)
        staged.rename(destination)


def _check_package(directory):
    if not (directory / "README.md").is_file():
        raise ValueError("Dataset Card README.md is missing")
    data = directory / "data"
    if not data.is_dir() or {path.name for path in data.iterdir()} != SCHEMAS.keys():
        raise ValueError(f"Expected exactly these tables under data/: {', '.join(SCHEMAS)}")
    for name, schema in SCHEMAS.items():
        shards = sorted((data / name).glob("*.parquet"))
        if not shards:
            raise ValueError(f"Missing Parquet shards for {name}")
        for shard in shards:
            if not pq.read_schema(shard).equals(schema):
                raise ValueError(f"Unexpected schema in {shard}")


def upload_dataset(directory, repo_id):
    from huggingface_hub import HfApi

    _check_package(directory)
    api = HfApi()
    url = api.create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True)
    info = api.repo_info(url.repo_id, repo_type="dataset")
    if info.private:
        raise ValueError(f"{url.repo_id} already exists as a private dataset")
    expected = {
        path.relative_to(directory).as_posix()
        for name in SCHEMAS
        for path in (directory / "data" / name).glob("*.parquet")
    }
    remote_shards = {
        path for path in api.list_repo_files(url.repo_id, repo_type="dataset")
        if path.startswith("data/") and path.endswith(".parquet")
    }
    if remote_shards - expected:
        raise ValueError("Target contains Parquet shards absent from staging. Use a clean "
                         "dataset repository or reconcile those files before uploading.")
    commit = api.upload_folder(
        repo_id=url.repo_id, repo_type="dataset", folder_path=directory,
        allow_patterns=["README.md", *sorted(expected)],
        commit_message="Publish the six arXiv project tables",
    )
    print(json.dumps({"dataset_url": str(url), "commit_url": commit.commit_url}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--dataset-dir", type=Path, required=True)
    stage.add_argument("--m1-export", type=Path, required=True)
    stage.add_argument("--m2-output", type=Path, required=True)
    stage.add_argument("--predictions", type=Path, required=True)
    stage.add_argument("--splits", type=Path, required=True)
    stage.add_argument("--labels", type=Path, required=True)
    stage.add_argument("--card", type=Path, default=DEFAULT_CARD)
    stage.add_argument("--shard-rows", type=int, default=50_000)
    upload = commands.add_parser("upload")
    upload.add_argument("--dataset-dir", type=Path, required=True)
    upload.add_argument("--repo-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "stage":
            stage_dataset(args.dataset_dir, args.m1_export, args.m2_output, args.predictions,
                          args.splits, args.labels, args.card, args.shard_rows)
            print(args.dataset_dir)
        else:
            upload_dataset(args.dataset_dir, args.repo_id)
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
