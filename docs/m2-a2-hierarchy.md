# M2 A2 — Version-local hierarchy

`parser.hierarchy.build_hierarchy(gathered)` consumes one A1 `GatheredVersion`
and returns `HierarchyResult(arxiv_id, version, status, root, bibliography,
diagnostics)`. It performs no M1 writes and does not export final M2 tables.

The root is a `document` node. Children appear in source order. Heading nodes
have `attrs.level` (`chapter=1`, `section=2`, `subsection=3`,
`subsubsection=4`, `paragraph=5`, `subparagraph=6`), `starred`, `appendix`, and
`title`. A skipped heading level does not create an artificial node. `list`
nodes own `item` nodes, including nested lists. Text between structural
commands becomes raw `paragraph` nodes; A3 will split it into finer leaves.
Each node has a deterministic, version-local `occurrence_id`, `raw`, ordered
`source_spans`, `children`, `attrs`, `content=None`, and `warnings`. A2 leaves
`content` for normalization after A3 and never overwrites `raw`. These occurrence IDs are
not final, deduplicated `element_id` values.

Only a recognized `thebibliography` environment is separated into
`BibliographyBlock(raw, source_spans)`. An immediately preceding empty
References/Bibliography heading is moved into the same block. An uncertain
References heading stays in the body with `uncertain_references` diagnostic.
An explicit `\bibliography{...}` or `\addbibresource{...}` command inside
the document body is also separated. Preamble `\addbibresource` remains in
A1 raw source for B2/C1. An unclosed `thebibliography` stays in the body with
`partial` and an `unclosed_protected` diagnostic.
`\printbibliography` is treated as an explicit bibliography boundary. A
References/Bibliography heading with introductory paragraphs is transferred
with a following explicit bibliography command. A heading with that title but
no clear boundary stays in the body with `partial` and
`uncertain_references`.
Acknowledgements and appendices remain in the body. `.bib` files still belong
to the A1 result; A2 does not parse them.

Blank lines inside recognized math, figure/table, verbatim and code
environments, and inside `$...$`, `$$...$$`, `\(...\)`, `\[...\]`, do not split
paragraphs. Unclosed protected regions retain their raw text and set
`partial` with a diagnostic. A3 receives these blocks in raw paragraph nodes
and owns fine-grained leaf extraction.
The protected environment set includes `algorithm` and `algorithm*` in
addition to figure/table/math/verbatim. Matching environment ends are found
outside comments and `\verb` spans; a missing end does not silently truncate
the block.

`unparseable` A1 input yields no root. `partial` A1 status propagates. A2 also
sets `partial` for unbalanced lists, orphan items, or headings encountered in
an open list. C3 decides whether partial nodes are published or retained only
for diagnostics. A2's scanner is static: it does not evaluate arbitrary TeX
macros, package-specific sectioning, or generated content.

```python
from parser.gather import gather_version
from parser.hierarchy import build_hierarchy

gathered = gather_version('2504.02523', 1, source_dir)
tree = build_hierarchy(gathered)
```
