# Milestone 3 evaluation and publishing

Run these commands from the project root. The module commands use PowerShell
line continuations and require the dependencies in `src/requirements.txt`.

```powershell
python -m pip install -r src/requirements.txt
$env:PYTHONPATH = "src"
$m1 = "runtime/exports/EXPORT_ID"
$m2 = "runtime/parsed"
$m3 = "runtime/matching"
```

Set `$m1` and `$m2` to the crawl export and parser output used by the
matching pipeline. The matching team provides these Parquet files under
`$m3` or supplies their actual paths.

| File | Required columns |
| --- | --- |
| `publication_split.parquet` (E1) | `arxiv_id`, `partition` |
| `groundtruth.parquet` (E1) | `arxiv_id`, `citation_key`, `groundtruth_arxiv_id` |
| `predictions_raw.parquet` (E2) | All nine prediction columns, with a plain string `partition` |

The split file has one row per participating publication. The model output
covers every BibTeX entry of those publications once. Ground truth contains
only labelled entries.
Use strings for identifiers and true Parquet nulls for absent ground truth
or candidates. Keep the model's order in the five candidate columns. A row
of null candidates represents an entry with no proposed candidates.

Build and validate the final table:

```powershell
python -m matching.validate_predictions `
  --output "$m3/predictions.parquet" --splits "$m3/publication_split.parquet" `
  --bibtex "$m2/bibtex.parquet" --references "$m1/references.parquet" `
  --labels "$m3/groundtruth.parquet" --model-output "$m3/predictions_raw.parquet"
```

To validate a final table supplied by the matching team, replace
`--output` with `--predictions` and omit `--model-output`. If the ranking
file contains only `arxiv_id`, `citation_key`, and the five candidate
columns, pass it with `--rankings` when building the final table.

Generate the MRR@5 report after validation succeeds:

```powershell
python -m matching.metrics `
  --predictions "$m3/predictions.parquet" --labels "$m3/groundtruth.parquet" `
  --out "$m3/mrr_report.json"
```

The report gives the MRR@5 and number of labelled entries independently for
`valid` and `test`. A labelled entry with null ground truth contributes zero
and counts toward the denominator. An entry absent from the labels file
is excluded from the score.

Stage the publishable Parquet shards and the [Dataset Card](huggingface/README.md)
in a new output directory:

```powershell
python -m matching.upload_hf stage `
  --dataset-dir "$m3/hf_dataset" --m1-export "$m1" --m2-output "$m2" `
  --predictions "$m3/predictions.parquet" --splits "$m3/publication_split.parquet" `
  --labels "$m3/groundtruth.parquet"
```

Stage validates the predictions against the selected sources. It exports
the five corpus tables in full, selecting the six publication fields for
`papers`, and partitions each table into shards of at most 50,000 rows by
default. Use `--shard-rows` to set a different row count. Use `--card` to
publish a card updated with the release provenance and real scores.

Authenticate to Hugging Face with a write-capable account, then upload to
the designated dataset repo:

```powershell
python -m matching.upload_hf upload `
  --dataset-dir "$m3/hf_dataset" --repo-id OWNER/DATASET
```

The command prints the dataset and commit URLs. Check the dataset in a
signed-out browser and open all six configs in the Viewer. With the
`datasets` package installed, check streaming:

```python
from datasets import load_dataset

for name in ("papers", "references", "elements", "hierarchy", "bibtex", "predictions"):
    rows = load_dataset("OWNER/DATASET", name, split="train", streaming=True)
    print(name, next(iter(rows), None))
```
