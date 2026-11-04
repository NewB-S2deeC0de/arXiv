# M2 A1 — Multi-file gathering

`parser.gather.gather_version(arxiv_id, version, source_dir)` reads one verified
M1 version's `source/tex/` directory. It does not modify M1 data. C1 is
responsible for choosing the version, checking its M1 status/marker and passing
the directory. `no_tex` is an empty version handled by the integration pipeline,
not an input to A1.

The result has `status` (`ready`, `partial`, or `unparseable`), `root_file`, ordered
`segments`, `used_tex_files`, `bib_files`, and `diagnostics`. Each segment's
`origin` is a path relative to `source_dir` and `[start,end)` Unicode character
offsets in that file's UTF-8 text. Repeated legal includes appear repeatedly
in segments but only once in `used_tex_files`. `.bib` paths are listed, not
parsed or asserted to be used by the compilation path.

```python
from pathlib import Path
from parser.gather import gather_version

result = gather_version('2504.02523', 1,
                        Path('runtime/data/2504.02523/versions/v1/source/tex'))
for segment in result.segments:
    print(segment.origin, segment.text[:80])
```

A1 recognizes `\documentclass` (including `[options]`) outside comments and
literal text, then expands braced `\input`/`\include` relative to the current
file. It ignores commented commands, `\verb`, and common verbatim environments.
For an extensionless include name it tries the literal file, then `.tex`.
It follows simple `\iftrue`/`\iffalse` branches, and applies a static
`\includeonly{a,b}` list. Unknown conditionals and dynamic `\includeonly`
lists are `partial`; their uncertain include targets are not expanded.
The root must contain one `\documentclass`, `\begin{document}`, and
`\end{document}` in that order to be `ready`.
No/multiple roots are `unparseable`. Missing, cyclic, dynamic or unsafe
includes, invalid UTF-8, and include chains deeper than 64 files are reported
with `partial` where useful content remains. Include commands that cannot be
expanded remain in source segments with diagnostics. External symlinks are not
read. A macro definition containing an include is kept verbatim and reported
as dynamic; ordinary definitions are kept without a warning. Structural
commands inside opaque command arguments are not expanded and are reported.
Each input file is limited to 32 MiB and expanded output to 64 MiB by default;
`max_file_bytes` and `max_output_bytes` can lower these bounds. Reaching a
bound produces a `resource_limit` diagnostic.

This is a static gatherer, not a TeX engine. It does not evaluate macro
expansion, arbitrary conditional expressions, generated files, or TeX search
paths. Source using those constructs may require manual review before claiming
exact PDF compilation coverage. A2/B1/B2/C1 interfaces are still provisional
in the out-of-repo coordination document.
