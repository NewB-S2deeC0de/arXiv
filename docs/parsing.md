# Milestone 2 parsing and standardization

This document describes the parser that turns the verified Milestone 1 LaTeX
sources into the three Milestone 2 tables. The parser is read-only with respect
to `runtime/data`, `runtime/progress` and `runtime/exports`; all output goes to
`runtime/parsed` (or `--out`).

This branch owns C1 (contract/adapter), C2 (cross-version deduplication) and C3
(pipeline/CLI/validation). A1/A2/A3/B1/B2/B3 are separate components loaded
through `parser.integration`; C3 tests inject a fake services object so the
orchestration can be verified before those components are merged.

## Contract and A/B boundary

The shared types and PyArrow schemas live in `src/parser/contract.py`. A
publication is stored once per `arxiv_id`; element ids use the publication key
with `.` and `/` replaced by `-`, followed by a kind prefix and counter:

```python
from parser.contract import (
    BIBTEX_SCHEMA, ELEMENTS_SCHEMA, HIERARCHY_SCHEMA,
    Element, HierarchyEdge, BibEntry, TreeNode, GatheredSource,
)

element = Element('2410-23025#s1', '2410.23025', 'sentence', 'Text.')
edge = HierarchyEdge('2410.23025', 1, '2410-23025#s1', '2410-23025#root1')
```

The A/B boundary is the `ParserServices` protocol:

| Method | Owner | Contract |
|:--|:--|:--|
| `gather(arxiv_id, version, source_dir)` | A1 | wraps `gather_version()`; returns `GatheredSource(text, status, root_file, files, bib_files, diagnostics, raw)` |
| `parse_document(gathered, arxiv_id, version)` | A2/A3 + B1 | returns `ParsedDocument(result=HierarchyResult, tree=None)`; `tree` is filled only after content is finalized by B1/remap |
| `extract_bibitems(text, arxiv_id)` | B2 | returns `list[BibEntry]` |
| `unify_bibentries(versioned)` | B3 | returns `(entries, citation_key_map, stats)` |
| `remap_citations(text, mapping)` | B3 | rewrites `\cite...` keys |

`DefaultParserServices.gather()` wraps the merged A1
`gather_version(arxiv_id, version, source_dir)` result and preserves the raw
`GatheredVersion` so A2 can consume its ordered segments and provenance.
`GatheredSource.status` is `ready`, `partial`, or `unparseable`; A1 diagnostics
are retained with their file/span when available.

`parser.integration.load_services()` adapts the real A/B modules. If they are not
merged yet, its `ParserDependencyError` explains how to pass `services=...` to
`run_parser()`.

## Entry points

```bash
PYTHONPATH=src python -m parser.cli --persistent-root runtime --limit 10
PYTHONPATH=src python -m parser.cli --persistent-root runtime
```

| Flag | Meaning |
|:--|:--|
| `--persistent-root` | Milestone 1 runtime root containing `data/` |
| `--out` | Output directory (default `<persistent-root>/parsed`) |
| `--limit N` | Process at most `N` publications, not versions |
| `--no-resume` | Discard `checkpoint.json` and `_parts/`, then reparse |
| `--quiet` | Suppress per-publication progress lines |

The same entry point is available from Python:

```python
from parser import run_parser
manifest = run_parser('runtime', limit=10)
```

`notebooks/parse_colab.ipynb` runs the parser on Colab with Drive persistence.

## Pipeline

1. **Read-only adapter (`adapter.py`).** Every `data/<arxiv_id>/versions/vN/source`
   is discovered, its `complete.json` marker is checked, and the
   `inventory.json` checksum is verified with the Milestone 1 digest. The adapter
   never rewrites or repairs Milestone 1 files. Missing source directories become
   `missing`; corrupt markers/checksums become `failed`; `no_tex` stays `no_tex`.
2. **Multi-file gathering (A1).** C1 passes the verified `source/tex/`
   directory to A1 `gather_version(arxiv_id, version, source_dir)`. A1 finds the
   single `\documentclass` root, follows static `\input`/`\include` directives in
   compilation order, and returns ordered segments with file spans plus
   diagnostics. Missing/unsafe includes, cycles, ambiguous roots, and decode
   failures become diagnostics; `unparseable` versions are recorded as failed,
   while `partial` versions are parsed and their diagnostics are kept as
   warnings. Raw A1 `GatheredVersion` is forwarded to A2, not just joined text.
3. **Hierarchy (A2/A3).** A2 consumes the raw A1 result via
   `build_hierarchy(gathered)` and returns a rich `HierarchyResult` with
   `ParseNode` trees, `BibliographyBlock`s, and `ParseDiagnostic`s. C3 keeps the
   rich tree/provenance until B1 finalizes content; it converts to C2 `TreeNode`
   only after normalization and citation remapping, and never falls back to raw
   as final content. A3 turns prose into sentence, block-math, figure/table and
   code leaves while preserving order, occurrence, and source spans. A2/A3
   diagnostics are normalized to multi-span records and written to manifest
   warnings.
4. **Standardization (B1 dependency).** B1 normalizes raw leaves without changing
   the tree or provenance. LaTeX commands, braces, spacing and math delimiters
   are normalized, and paragraphs are split into sentences without breaking
   mathematics, initials, abbreviations, decimals, or URLs. `\cite` commands are
   preserved for reference remapping. BibTeX entries are taken from
   `HierarchyResult.bibliography` first, falling back to gathered text only when
   no block is exposed.
5. **Elements and cross-version deduplication (`dedup.py`, C2).** A publication
   has a single element store. Content equality is evaluated *after* B1 cleanup;
   an optional `normalize` callback compares content without rewriting the stored
   text. Identical content across versions reuses one `element_id`; a repeated
   identical element inside the same version keeps a new id so the structure is
   not collapsed. Document roots are always unique per version. Hierarchy edges
   are stored per version, so a shared element can hang under different parents
   in different versions.
6. **BibTeX unification (B2/B3 dependency).** `\bibitem` entries are extracted
   from the full gathered text, including the bibliography. Entries with the same
   normalized title (or, when the title is missing, the same key) are merged;
   fields are unioned. Dropped citation keys are remapped and every
   `\cite`/`\citep`/`\citet` argument is rewritten to the chosen key before
   elements are deduplicated.
7. **Output (`pipeline.py`, C3).** Each publication is parsed and written to a
   shard under `_parts/<encoded-id>/`. A checkpoint is flushed every 25
   publications and at the end. At the end the shards are streamed into the final
   Parquet files, so the whole dataset is never loaded into RAM. A read-only guard
   rejects outputs under `data/`, `progress/`, or `exports/`.
   `partial` versions are recorded with diagnostics and coverage but are not
   written to the three tables by default; use `include_partial=True` to opt in.
   `unparseable` and `no_tex` versions are recorded separately and never abort
   remaining publications.
8. **Validation (`validation.py`, C3).** Before a shard is written, the
   publication is checked for unique `element_id`s, valid kinds, hierarchy foreign
   keys in the same publication, positive versions, duplicate edges, and duplicate
   BibTeX keys. Missing citation keys are recorded as warnings. A1/A2
   diagnostics are normalized into `gather_warning`/`parse_warning` entries and
   written to `manifest.json`. After merging, the Parquet schemas are checked
   against C1. Structural errors raise `ValidationError`.

## Output layout

```text
runtime/parsed/
|-- elements.parquet
|-- hierarchy.parquet
|-- bibtex.parquet
|-- coverage.csv
|-- errors.json
|-- checkpoint.json
|-- manifest.json
`-- _parts/<encoded-arxiv-id>/{elements,hierarchy,bibtex}.parquet
```

`elements`, `hierarchy`, and `bibtex` use the schemas from `contract.py` and
match the Milestone 2 specification. `coverage.csv` has one row per publication
with versions total/parsed/empty/failed/missing, gather failures, element and
BibTeX counts before/after deduplication, and parse time in seconds.

`manifest.json` contains:

- `papers_total` / `papers_processed`, version and empty-version counts;
- element and BibTeX counts before/after deduplication;
- `timing.elapsed_seconds` and `timing.average_seconds_per_publication`;
- `memory.peak_rss_bytes` (when `resource` is available, e.g. Colab Linux);
- `storage.output_bytes` and `_parts` bytes;
- `outputs.<table>.{rows,sha256,bytes}` checksums;
- `fingerprints.{config,input,schema}` so a run can be reproduced and compared;
- `validation.{ok,errors,warnings,publications_checked,rows}`;
- `software` versions and `errors` (also in `errors.json`).

## Resume semantics

- A publication is skipped only when it is in `checkpoint.json` *and* all three
  shards exist. After an abrupt stop, at most the last checkpoint window (25
  publications) is reparsed; existing shards are overwritten safely.
- `--no-resume` deletes the checkpoint and `_parts/`, then reparses from scratch.
- `--limit N` limits both processing and the final merged tables to the first `N`
  publications; older shards may remain in `_parts/` but are not merged.
- Failed/missing versions are recorded in coverage and `errors.json`; they do not
  abort the remaining publications.
- The parser does not modify Milestone 1 state, so it can run while the crawler
  is stopped and does not need the crawler dataset lock.

## Tests

C1/C2/C3 tests are independent of A/B:

```bash
PYTHONPATH=src python -m pytest \
  tests/test_parser_contract.py tests/test_parser_adapter.py \
  tests/test_parser_dedup.py tests/test_parser_pipeline.py \
  tests/test_parser_validation.py tests/test_parser_integration.py \
  tests/test_a1_gather.py -q
```

They use synthetic Milestone 1 runtimes, fake A/B services, and a small real-A1
fixture; no network is used. They cover adapter statuses, schemas/ids,
cross-version deduplication, pipeline orchestration, A1 ready/partial/
unparseable conversion, A1/A2 diagnostics, validation, manifest/checksums,
resume skip, `--limit` at publication level, rerun stability, and the read-only
output guard.

## Limitations

- A1 is merged. A2 is implemented on `feat/m2-a2`; A3/B1/B2/B3 are still
  separate. Until they merge into this branch, `load_services()` raises
  `ParserDependencyError`; C3 orchestration can still be exercised by injecting
  `services=...`.
- The default A2 adapter forwards the raw A1 `GatheredVersion` and maps A2
  `ParseNode.raw` to C3 content. A3/B1 will replace that provisional mapping with
  the final normalized content and warning dispatch.
- Parsing is heuristic: custom macros, exotic environments, and malformed TeX can
  produce imperfect hierarchy or cleanup results. Parser failures are reported per
  version instead of being silently ignored.
- Missing citation keys are warnings, not hard failures, because sources may
  contain intentionally undefined references.
- `manifest.parser_policy.policy_fingerprint` hashes parser version, schemas, and
  `include_partial` policy. After changing A2/A3/B1 or the partial policy, old
  shards must not be reused as the same parser version; resume should be reset.
- `peak_rss_bytes` is process peak RSS, not a per-publication measurement.
- BibTeX extraction handles `\bibitem`-style bibliographies. `\bibitem`-free
  setups that rely on external `.bib` files still produce hierarchy content but
  no `bibtex` rows; `.bib` parsing is not part of this milestone.
