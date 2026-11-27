---
configs:
  - config_name: papers
    data_files:
      - split: train
        path: "data/papers/*.parquet"
  - config_name: references
    data_files:
      - split: train
        path: "data/references/*.parquet"
  - config_name: elements
    data_files:
      - split: train
        path: "data/elements/*.parquet"
  - config_name: hierarchy
    data_files:
      - split: train
        path: "data/hierarchy/*.parquet"
  - config_name: bibtex
    data_files:
      - split: train
        path: "data/bibtex/*.parquet"
  - config_name: predictions
    data_files:
      - split: train
        path: "data/predictions/*.parquet"
---

# arXiv citation matching

This course project combines arXiv paper metadata, references from Semantic
Scholar, and structured elements parsed from LaTeX sources. Its matching
task ranks references for each standardized BibTeX entry within the citing
publication.

| Configuration | Contents |
| --- | --- |
| `papers` | Metadata keyed by `arxiv_id` |
| `references` | Cited papers keyed by `(arxiv_id, ref_arxiv_id)` |
| `elements` | Deduplicated document elements |
| `hierarchy` | Parent-child edges for each publication version |
| `bibtex` | Standardized entries keyed by `(arxiv_id, citation_key)` |
| `predictions` | Ground truth and up to five ranked references per entry in the matching partitions |

Each configuration can be loaded with `split="train"` and `streaming=True`.
For the matching task, use the `partition` column of `predictions` to select
`train`, `valid`, or `test`. Both ground truth and candidate IDs refer to the
`ref_arxiv_id` values of the same citing publication. Null ground truth marks
a labelled entry with no matching reference. Null candidate cells represent
unfilled ranks.

MRR@5 averages the reciprocal rank of the correct reference over labelled
BibTeX entries. An entry with null ground truth or without a correct reference
among the five candidates contributes zero.
