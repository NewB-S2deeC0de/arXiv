# M2 A3 — Sentence, formula and figure leaves

`parser.leaves.refine_hierarchy(tree)` accepts an A2 `HierarchyResult` for one
publication version and returns a new result. It deep copies the A2 result;
the input and separated bibliography are unchanged. It performs no M1 writes.

```python
from parser import gather_version, build_hierarchy, refine_hierarchy

gathered = gather_version('2504.02523', 1, source_dir)
tree = refine_hierarchy(build_hierarchy(gathered))
```

Existing `document`, `heading`, `list`, and `item` parents retain their order
and occurrence IDs. Ordinary paragraphs retain their original ID and acquire
`sentence` children. A paragraph containing display blocks is replaced by
ordered prose paragraphs and `block_formula` or `figure` leaves. New IDs start
after the maximum A2 occurrence ID and are deterministic within the version.
These IDs are local occurrences, not final element IDs.

`block_formula` covers `equation`, `align`, `gather`, `multline`,
`displaymath` (including starred variants), `$$…$$`, and `\[…\]`.
Each environment is one leaf, with its label/tag and original delimiters in
`raw`. `figure`, `table`, and `algorithm` environments become `figure` leaves;
`attrs.subtype` distinguishes them, and `attrs.caption`/`attrs.label` are
best effort extractions. Caption text remains in the figure's `raw` only and
does not also become a sentence. Bibliography blocks are not processed by A3.

Sentence splitting is conservative around common abbreviations, URLs,
decimal numbers, comments, LaTeX command arguments, cites and inline math.
Inline `$…$` and `\(…\)` remain in sentence raw. Each leaf keeps exact raw
spelling and slices its `source_spans` from the A2 paragraph, including spans
across included files. `content` stays `None`; B1 normalization belongs after
A3 and must preserve `raw` and provenance. C can call A3 before B1 in its
version pipeline.

Unknown literal/code environments, unclosed blocks, and inconsistent span
lengths retain their entire raw paragraph. The result becomes `partial` and
gets a diagnostic (`unsupported_literal`, `unclosed_block`, `unclosed_math`,
or `span_mismatch`). C decides whether to publish partial output. This static
scanner does not expand TeX macros or prove linguistic sentence boundaries.
