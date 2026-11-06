import csv
import json
import re
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from crawler.storage import digest
from parser.contract import (BibEntry, BibliographyBlock, GatherDiagnostic,
                             GatheredSource, GatherError, HierarchyResult,
                             ParseDiagnostic, ParsedDocument, SourceSpan,
                             TreeNode)
from parser.pipeline import run_parser

V1 = r'''
\documentclass{article}
\title{Paper A}
\begin{document}
\section{Introduction}
Shared sentence. Only v1 sentence with \cite{old}.
\begin{thebibliography}{1}
\bibitem{old} A. Author. Same Title. 2020.
\end{thebibliography}
\end{document}
'''

V2 = r'''
\documentclass{article}
\title{Paper A}
\begin{document}
\section{Introduction}
Shared sentence. Only v2 sentence with \cite{new}.
\begin{thebibliography}{1}
\bibitem{new} A. Author. Same Title. 2021.
\end{thebibliography}
\end{document}
'''

CITE_RE = re.compile(
    r'(\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|nocite)'
    r'\*?(?:\[[^\]]*\])*\{)([^}]*)(\})'
)


class FakeServices:
    """Minimal A/B stand-in so C3 orchestration is tested independently."""

    def gather(self, arxiv_id, version, source_dir):
        root = Path(source_dir)
        files = tuple(sorted(root.rglob('*.tex')))
        if not files:
            raise GatherError('no tex files')
        text = '\n'.join(path.read_text(encoding='utf8') for path in files)
        return GatheredSource(
            text=text, status='ready', root_file=files[0].name,
            files=tuple(path.relative_to(root).as_posix() for path in files),
        )

    def parse_document(self, gathered, arxiv_id, version):
        body = gathered.text.split(r'\begin{document}', 1)[-1]
        body = body.split(r'\end{document}', 1)[0]
        body = body.split(r'\begin{thebibliography}', 1)[0]
        body = re.sub(r'\\(?:sub)*section\*?(?:\[[^\]]*\])?\{[^}]*\}', '', body)
        body = re.sub(r'\\title(?:\[[^\]]*\])?\{[^}]*\}', '', body)
        sentences = [part.strip() for part in re.split(r'(?<=[.!?])\s+', body)
                     if part.strip()]
        root = TreeNode('document', 'Paper A')
        root.children = [TreeNode('sentence', sentence) for sentence in sentences]
        return ParsedDocument(
            result=HierarchyResult(
                arxiv_id=arxiv_id, version=version, status='ready', root=None,
                bibliography=(), diagnostics=(),
            ),
            tree=root,
        )

    def extract_bibitems(self, text, arxiv_id):
        entries = []
        for match in re.finditer(r'\\bibitem\{([^}]*)\}([^\n]*)', text):
            key = match.group(1).strip()
            body = match.group(2).strip()
            parts = [part.strip() for part in body.rstrip('.').split('.') if part.strip()]
            year = None
            if parts and re.fullmatch(r'(?:19|20)\d{2}', parts[-1]):
                year = int(parts.pop())
            title = parts.pop() if parts else None
            entries.append(BibEntry(arxiv_id, key, title=title, authors=parts, year=year))
        return entries

    def unify_bibentries(self, versioned):
        groups = {}
        order = []
        for version, entry in versioned:
            signature = (entry.title or entry.citation_key).lower()
            if signature not in groups:
                groups[signature] = []
                order.append(signature)
            groups[signature].append((version, entry))
        chosen = []
        mapping = {}
        duplicates = 0
        for signature in order:
            items = sorted(groups[signature], key=lambda item: (item[0], item[1].citation_key))
            merged = items[0][1]
            for _, entry in items[1:]:
                duplicates += 1
                mapping[entry.citation_key] = merged.citation_key
                merged = BibEntry(
                    merged.arxiv_id,
                    merged.citation_key,
                    entry_type=entry.entry_type if merged.entry_type == 'misc' else merged.entry_type,
                    title=merged.title or entry.title,
                    authors=merged.authors + [author for author in entry.authors
                                              if author not in merged.authors],
                    year=merged.year if merged.year is not None else entry.year,
                    doi=merged.doi or entry.doi,
                    journal=merged.journal or entry.journal,
                    url=merged.url or entry.url,
                )
            mapping.setdefault(merged.citation_key, merged.citation_key)
            chosen.append(merged)
        return chosen, mapping, dict(before=len(versioned), after=len(chosen),
                                     duplicates=duplicates)

    def remap_citations(self, text, mapping):
        def replace(match):
            keys = [key.strip() for key in match.group(2).split(',')]
            return match.group(1) + ', '.join(mapping.get(key, key) for key in keys) + match.group(3)

        return CITE_RE.sub(replace, text)

    def cache_fingerprint(self):
        return 'fake-services-v1'


class PartialServices(FakeServices):
    def gather(self, arxiv_id, version, source_dir):
        base = super().gather(arxiv_id, version, source_dir)
        diagnostic = GatherDiagnostic(
            'missing_include', 'Missing child.tex', 'warning', 'main.tex', 0, 1,
        )
        return GatheredSource(
            text=base.text, status='partial', root_file=base.root_file,
            files=base.files, bib_files=base.bib_files,
            diagnostics=(diagnostic,),
        )


class UnparseableServices(FakeServices):
    def gather(self, arxiv_id, version, source_dir):
        return GatheredSource(
            text='', status='unparseable',
            diagnostics=(GatherDiagnostic('no_root', 'Root candidates: []', 'error'),),
        )


class PartialParseServices(FakeServices):
    def parse_document(self, gathered, arxiv_id, version):
        parsed = super().parse_document(gathered, arxiv_id, version)
        diagnostic = ParseDiagnostic(
            'uncertain_references', 'References heading kept in body',
            'warning', source_spans=(),
        )
        return ParsedDocument(
            result=HierarchyResult(
                arxiv_id=arxiv_id, version=version, status='partial', root=None,
                bibliography=(), diagnostics=(diagnostic,),
            ),
            tree=parsed.tree,
        )


@pytest.fixture
def services():
    return FakeServices()


def _snapshot(root):
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted((root / 'data').rglob('*')) if path.is_file()}


@pytest.fixture
def runtime(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')
    make_source(root, '2410.23025', 2, {'main.tex': V2}, 'succeeded')
    make_source(root, '2410.23026', 1, {}, 'no_tex')
    return root


def test_pipeline_writes_typed_tables_and_manifest(runtime, services):
    before = _snapshot(runtime)
    manifest = run_parser(runtime, services=services)

    assert manifest['papers_total'] == 2 and manifest['papers_processed'] == 2
    assert manifest['versions'] == dict(versions_total=3, versions_parsed=2,
                                        versions_empty=1, versions_partial=0,
                                        versions_pending=0, versions_failed=0,
                                        missing=0, gather_failed=0)
    assert manifest['elements']['before_dedup'] > manifest['elements']['after_dedup']
    assert manifest['bibtex']['before_dedup'] == 2
    assert manifest['bibtex']['after_dedup'] == 1
    assert manifest['bibtex']['deduplicated'] == 1
    assert manifest['read_only_m1'] is True
    assert manifest['validation']['ok'] is True
    assert manifest['validation']['warnings'] == []
    assert manifest['fingerprints']['input'] and manifest['fingerprints']['schema']
    assert all(key in manifest['outputs'] for key in
               ('elements', 'hierarchy', 'bibtex', 'coverage.csv'))

    output = runtime / 'parsed'
    elements = pq.read_table(output / 'elements.parquet')
    assert elements.column_names == ['element_id', 'arxiv_id', 'kind', 'content']
    rows = elements.to_pylist()
    shared = [row for row in rows if row['content'] == 'Shared sentence.']
    assert len(shared) == 1
    assert all(row['element_id'].startswith('2410-23025#') for row in rows)

    hierarchy = pq.read_table(output / 'hierarchy.parquet').to_pylist()
    shared_id = shared[0]['element_id']
    assert {edge['version'] for edge in hierarchy if edge['element_id'] == shared_id} == {1, 2}

    bibtex = pq.read_table(output / 'bibtex.parquet').to_pylist()
    assert len(bibtex) == 1
    assert bibtex[0]['citation_key'] == 'old'
    assert bibtex[0]['year'] == 2020
    assert bibtex[0]['title'] == 'Same Title'

    assert manifest['outputs']['elements']['sha256'] == digest(output / 'elements.parquet')
    assert json.loads((output / 'manifest.json').read_text(encoding='utf8')) == manifest
    assert json.loads((output / 'errors.json').read_text(encoding='utf8')) == []
    with (output / 'coverage.csv').open(encoding='utf8') as handle:
        coverage = list(csv.DictReader(handle))
    assert {row['arxiv_id'] for row in coverage} == {'2410.23025', '2410.23026'}

    assert _snapshot(runtime) == before
    assert not (runtime / 'progress').exists()
    assert not (runtime / 'exports').exists()


def test_pipeline_reports_partial_gather_diagnostics(runtime):
    manifest = run_parser(runtime, services=PartialServices())
    warnings = manifest['validation']['warnings']
    gather_warnings = [item for item in warnings if item['status'] == 'gather_warning']
    assert len(gather_warnings) == 2
    assert {item['code'] for item in gather_warnings} == {'missing_include'}
    assert manifest['versions']['versions_parsed'] == 0
    assert manifest['versions']['versions_partial'] == 2
    assert manifest['versions']['versions_failed'] == 0


def test_pipeline_includes_partial_when_flagged(runtime):
    manifest = run_parser(runtime, services=PartialServices(), include_partial=True)
    assert manifest['versions']['versions_parsed'] == 2
    assert manifest['versions']['versions_partial'] == 0
    assert manifest['versions']['versions_failed'] == 0


def test_pipeline_fails_unparseable_gather(runtime):
    manifest = run_parser(runtime, services=UnparseableServices())
    assert manifest['versions']['versions_total'] == 3
    assert manifest['versions']['gather_failed'] == 2
    assert manifest['versions']['versions_failed'] == 2
    assert manifest['versions']['versions_parsed'] == 0
    errors = json.loads((runtime / 'parsed' / 'errors.json').read_text(encoding='utf8'))
    assert len(errors) == 2
    assert all('gather:unparseable' in item['error'] for item in errors)


def test_pipeline_reports_partial_parse_diagnostics(runtime):
    manifest = run_parser(runtime, services=PartialParseServices(), include_partial=True)
    warnings = manifest['validation']['warnings']
    parse_warnings = [item for item in warnings if item['status'] == 'parse_warning']
    assert len(parse_warnings) == 2
    assert {item['code'] for item in parse_warnings} == {'uncertain_references'}
    assert manifest['versions']['versions_parsed'] == 2
    assert manifest['versions']['versions_failed'] == 0


def test_pipeline_dedups_elements_after_citation_remap(tmp_path, make_source, services):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23027', 1, {'main.tex': r'''
\documentclass{article}
\begin{document}
Shared claim \cite{old}. Unique v1.
\begin{thebibliography}{1}
\bibitem{old} A. Author. Same Title. 2020.
\end{thebibliography}
\end{document}
'''}, 'succeeded')
    make_source(root, '2410.23027', 2, {'main.tex': r'''
\documentclass{article}
\begin{document}
Shared claim \cite{new}. Unique v2.
\begin{thebibliography}{1}
\bibitem{new} A. Author. Same Title. 2021.
\end{thebibliography}
\end{document}
'''}, 'succeeded')
    manifest = run_parser(root, services=services)
    rows = pq.read_table(root / 'parsed' / 'elements.parquet').to_pylist()
    sentence_rows = [row for row in rows if row['content'] == r'Shared claim \cite{old}.']
    assert len(sentence_rows) == 1
    shared_id = sentence_rows[0]['element_id']
    hierarchy = pq.read_table(root / 'parsed' / 'hierarchy.parquet').to_pylist()
    assert {edge['version'] for edge in hierarchy if edge['element_id'] == shared_id} == {1, 2}
    assert manifest['bibtex']['deduplicated'] == 1


def test_pipeline_resume_skips_checkpointed_publications(runtime, services):
    run_parser(runtime, services=services)
    log = []
    manifest = run_parser(runtime, services=services, log=log.append)
    assert sum('skip' in line for line in log) == 2
    assert manifest['papers_processed'] == 2


def test_pipeline_limit_counts_publications(runtime, services):
    manifest = run_parser(runtime, output_root=runtime / 'parsed_limit', limit=1,
                          services=services)
    assert manifest['papers_total'] == 1
    assert manifest['papers_processed'] == 1
    assert manifest['versions']['versions_total'] == 2


def test_pipeline_limit_restricts_existing_output(runtime, services):
    run_parser(runtime, services=services)
    manifest = run_parser(runtime, limit=1, services=services)
    assert manifest['papers_total'] == 1 and manifest['papers_processed'] == 1
    rows = pq.read_table(runtime / 'parsed' / 'elements.parquet').to_pylist()
    assert rows and all(row['arxiv_id'] == '2410.23025' for row in rows)


def test_pipeline_rerun_is_stable(runtime, services):
    first = run_parser(runtime, output_root=runtime / 'parsed_a', services=services)
    second = run_parser(runtime, output_root=runtime / 'parsed_b', services=services)
    for name in ('elements', 'hierarchy', 'bibtex'):
        assert first['outputs'][name] == second['outputs'][name]
    assert first['validation'] == second['validation']


def test_pipeline_empty_runtime_does_not_require_ab_services(tmp_path):
    manifest = run_parser(tmp_path / 'runtime')
    assert manifest['papers_total'] == 0
    assert manifest['papers_processed'] == 0
    assert manifest['validation']['ok'] is True
    assert (tmp_path / 'runtime' / 'parsed' / 'elements.parquet').is_file()


def test_partial_only_version_produces_no_rows_when_not_included(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')
    manifest = run_parser(root, services=PartialServices())
    assert manifest['versions']['versions_partial'] == 1
    assert manifest['versions']['versions_parsed'] == 0
    output = root / 'parsed'
    assert pq.read_table(output / 'elements.parquet').num_rows == 0
    assert pq.read_table(output / 'hierarchy.parquet').num_rows == 0
    assert pq.read_table(output / 'bibtex.parquet').num_rows == 0
    warnings = manifest['validation']['warnings']
    assert any(w['stage'] == 'gather' and w['code'] == 'missing_include'
               for w in warnings)


def test_partial_only_version_exported_when_flagged(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')
    manifest = run_parser(root, services=PartialServices(), include_partial=True)
    assert manifest['versions']['versions_parsed'] == 1
    assert manifest['versions']['versions_partial'] == 0
    output = root / 'parsed'
    assert pq.read_table(output / 'elements.parquet').num_rows >= 1
    assert pq.read_table(output / 'bibtex.parquet').num_rows == 1
    assert pq.read_table(output / 'bibtex.parquet').to_pylist()[0]['citation_key'] == 'old'


def test_resume_skips_same_policy(tmp_path, make_source, services):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')
    run_parser(root, services=services)
    log = []
    run_parser(root, services=services, log=log.append)
    assert any('skip' in line for line in log), log


def test_resume_reprocesses_on_partial_policy_change(tmp_path, make_source, services):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')
    log1 = []
    run_parser(root, services=services, log=log1.append)
    assert any('parse' in line and 'skip' not in line for line in log1), log1
    log2 = []
    run_parser(root, services=services, include_partial=True, log=log2.append)
    assert any('parse' in line and 'skip' not in line for line in log2), log2


def test_parse_warning_keeps_all_spans(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class MultiSpanServices(FakeServices):
        def parse_document(self, gathered, arxiv_id, version):
            base = super().parse_document(gathered, arxiv_id, version)
            diagnostic = ParseDiagnostic(
                'multi_span', 'references two files', 'warning',
                source_spans=(SourceSpan('main.tex', 0, 5),
                              SourceSpan('part.tex', 10, 15)),
            )
            result = HierarchyResult(
                arxiv_id=arxiv_id, version=version, status='ready', root=None,
                bibliography=(), diagnostics=(diagnostic,),
            )
            return ParsedDocument(result=result, tree=base.tree)

    manifest = run_parser(root, services=MultiSpanServices(), include_partial=True)
    warnings = manifest['validation']['warnings']
    parse_warnings = [w for w in warnings if w.get('stage') == 'parse']
    assert len(parse_warnings) == 1
    assert len(parse_warnings[0]['spans']) == 2
    assert parse_warnings[0]['spans'][0] == {'path': 'main.tex', 'start': 0, 'end': 5}
    assert parse_warnings[0]['spans'][1] == {'path': 'part.tex', 'start': 10, 'end': 15}


def test_ready_tree_none_bibtex_not_exported(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class TreeNoneServices(FakeServices):
        def parse_document(self, gathered, arxiv_id, version):
            block = BibliographyBlock(raw=gathered.text,
                                      source_spans=(SourceSpan('main.tex', 0, 5),))
            result = HierarchyResult(
                arxiv_id=arxiv_id, version=version, status='ready', root=None,
                bibliography=(block,), diagnostics=(),
            )
            return ParsedDocument(result=result, tree=None)

    manifest = run_parser(root, services=TreeNoneServices(), include_partial=True)
    assert manifest['versions']['versions_failed'] == 0
    assert manifest['versions']['versions_parsed'] == 0
    assert manifest['versions']['versions_pending'] == 1
    output = root / 'parsed'
    assert pq.read_table(output / 'elements.parquet').num_rows == 0
    assert pq.read_table(output / 'hierarchy.parquet').num_rows == 0
    assert pq.read_table(output / 'bibtex.parquet').num_rows == 0


def test_partial_tree_none_bibtex_not_exported(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class PartialTreeNoneServices(FakeServices):
        def parse_document(self, gathered, arxiv_id, version):
            block = BibliographyBlock(raw=gathered.text,
                                      source_spans=(SourceSpan('main.tex', 0, 5),))
            result = HierarchyResult(
                arxiv_id=arxiv_id, version=version, status='partial', root=None,
                bibliography=(block,), diagnostics=(),
            )
            return ParsedDocument(result=result, tree=None)

    manifest = run_parser(root, services=PartialTreeNoneServices(), include_partial=True)
    assert manifest['versions']['versions_failed'] == 0
    assert manifest['versions']['versions_parsed'] == 0
    assert manifest['versions']['versions_pending'] == 1
    output = root / 'parsed'
    assert pq.read_table(output / 'elements.parquet').num_rows == 0
    assert pq.read_table(output / 'hierarchy.parquet').num_rows == 0
    assert pq.read_table(output / 'bibtex.parquet').num_rows == 0


def test_resume_reprocesses_on_service_fingerprint_change(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class ServiceV1(FakeServices):
        def cache_fingerprint(self):
            return 'service-v1'

    class ServiceV2(FakeServices):
        def cache_fingerprint(self):
            return 'service-v2'

    log1 = []
    run_parser(root, services=ServiceV1(), log=log1.append)
    assert any('skip' not in line and 'parse' in line for line in log1), log1

    log2 = []
    run_parser(root, services=ServiceV1(), log=log2.append)
    assert any('skip' in line for line in log2), log2

    log3 = []
    run_parser(root, services=ServiceV2(), log=log3.append)
    assert any('skip' not in line and 'parse' in line for line in log3), log3


def test_resume_reprocesses_when_service_config_changes(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class ConfigurableServices(FakeServices):
        def __init__(self, suffix):
            self.suffix = suffix

        def cache_fingerprint(self):
            return f'ConfigurableServices:v1:{self.suffix}'

        def parse_document(self, gathered, arxiv_id, version):
            parsed = super().parse_document(gathered, arxiv_id, version)
            parsed.tree.children[0].content += self.suffix
            return parsed

    log1 = []
    run_parser(root, services=ConfigurableServices(' A'), log=log1.append)
    assert any('skip' not in line and 'parse' in line for line in log1), log1

    log2 = []
    run_parser(root, services=ConfigurableServices(' A'), log=log2.append)
    assert any('skip' in line for line in log2), log2

    log3 = []
    run_parser(root, services=ConfigurableServices(' B'), log=log3.append)
    assert any('skip' not in line and 'parse' in line for line in log3), log3
    contents = [row['content'] for row in
                pq.read_table(root / 'parsed' / 'elements.parquet').to_pylist()]
    assert any(' B' in content for content in contents), contents


def test_resume_disabled_for_service_without_fingerprint(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    class NoFingerprintServices(FakeServices):
        cache_fingerprint = None

    log1 = []
    run_parser(root, services=NoFingerprintServices(), log=log1.append)
    assert any('skip' not in line and 'parse' in line for line in log1), log1

    log2 = []
    manifest = run_parser(root, services=NoFingerprintServices(), log=log2.append)
    assert any('skip' not in line and 'parse' in line for line in log2), log2
    assert manifest['resume'] == dict(
        requested=True, effective=False,
        disabled_reason='service_without_cache_fingerprint',
    )


def test_input_fingerprint_changes_when_content_changes(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1, 'refs.bib': '@misc{x}'}, 'succeeded')

    from parser.pipeline import _publication_input_fingerprint
    from parser.adapter import read_source_record
    version_dir = root / 'data' / '2410.23025' / 'versions' / 'v1'
    recs = [read_source_record('2410.23025', 1, version_dir)]

    import os
    tex = version_dir / 'source' / 'tex' / 'main.tex'

    fp_a = _publication_input_fingerprint('2410.23025', recs)
    tex.write_text('xxxxxx', encoding='utf8')
    fp_b = _publication_input_fingerprint('2410.23025', recs)
    tex.write_text('yyyyyy', encoding='utf8')
    fp_c = _publication_input_fingerprint('2410.23025', recs)

    assert fp_a != fp_b != fp_c


def test_input_fingerprint_changes_with_selected_bib_and_approvals(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1, 'refs.bib': '@misc{x}'}, 'succeeded')

    from parser.pipeline import _publication_input_fingerprint
    from parser.adapter import read_source_record
    version_dir = root / 'data' / '2410.23025' / 'versions' / 'v1'
    recs = [read_source_record('2410.23025', 1, version_dir)]

    base = _publication_input_fingerprint('2410.23025', recs)
    selected = _publication_input_fingerprint(
        '2410.23025', recs,
        selected_bib_files={('2410.23025', 1): ('refs.bib',)})
    approved = _publication_input_fingerprint(
        '2410.23025', recs,
        approved_metadata_candidates={'2410.23025': ('candidate-1',)})

    assert len({base, selected, approved}) == 3


def test_pipeline_reprocesses_when_audit_sidecar_is_missing(runtime, services):
    run_parser(runtime, services=services)
    audit = runtime / 'parsed' / '_parts' / '2410.23025' / 'audit.json'
    assert audit.is_file()
    audit.unlink()

    log = []
    run_parser(runtime, services=services, log=log.append)

    assert not any('skip 2410.23025' in line for line in log)
    assert audit.is_file()


def test_pipeline_rejects_writes_into_milestone1(runtime, services):
    with pytest.raises(ValueError, match='Milestone 1'):
        run_parser(runtime, output_root=runtime / 'data' / 'parsed', services=services)
    with pytest.raises(ValueError, match='Milestone 1'):
        run_parser(runtime, output_root=runtime / 'progress' / 'parsed', services=services)

REAL_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
We use \cite{alpha} and \cite{beta}.
\bibliography{refs}
\end{document}
"""


def test_default_services_run_real_a_to_b_to_c_integration(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2411.18799', 1, {
        'main.tex': REAL_TEX,
        'refs.bib': ('@article{alpha, title={Alpha}, year={2020}}\n'
                     '@article{beta, title={Beta}, year={2021}}'),
    }, 'succeeded')

    manifest = run_parser(root)

    assert manifest['versions']['versions_parsed'] == 1
    assert manifest['versions']['versions_failed'] == 0
    assert manifest['bibtex']['before_dedup'] == 2
    assert manifest['bibtex']['after_dedup'] == 2
    assert manifest['bibtex']['deduplicated'] == 0
    assert manifest['quality']['citations_unresolved'] == 0
    assert manifest['audit']['publications'] == 1
    bibtex = pq.read_table(root / 'parsed' / 'bibtex.parquet').to_pylist()
    assert len(bibtex) == 2
    assert {row['title'] for row in bibtex} == {'Alpha', 'Beta'}
    elements = pq.read_table(root / 'parsed' / 'elements.parquet').to_pylist()
    assert any('\\cite{' in row['content'] for row in elements)
    audit = json.loads((root / 'parsed' / 'bibtex_audit.json').read_text(encoding='utf8'))
    publication = audit['publications']['2411.18799']
    assert publication['mode'] == 'b_standardization'
    assert publication['versions'][0]['status'] == 'ready'
    assert publication['references']['result']['merge']['records']
    assert (root / 'parsed' / '_parts' / '2411.18799' / 'audit.json').is_file()


def test_default_services_missing_bib_is_partial_not_pending(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2411.18799', 1, {
        'main.tex': r"""\documentclass{article}\begin{document}
We use \cite{alpha}. \bibliography{missing}
\end{document}""",
    }, 'succeeded')

    manifest = run_parser(root)

    assert manifest['versions']['versions_partial'] == 1
    assert manifest['versions']['versions_pending'] == 0
    assert manifest['versions']['versions_parsed'] == 0
    assert any(w['code'] == 'bibliography_dependency_not_in_listed_bib_files'
               for w in manifest['validation']['warnings'])


TWO_VERSION_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
The shared claim is stable. Version-specific result \cite{same}.
\bibliography{refs}
\end{document}
"""


def test_default_services_remap_same_key_per_version_not_globally(tmp_path, make_source):
    root = tmp_path / 'runtime'
    for version in (1, 2):
        make_source(root, '2411.18799', version, {
            'main.tex': TWO_VERSION_TEX,
            'refs.bib': (f'@article{{same, title={{Version {version}}}, '
                         f'doi={{10.1234/ref{version}}}}}'),
        }, 'succeeded')

    manifest = run_parser(root)

    assert manifest['versions']['versions_parsed'] == 2
    bibtex = pq.read_table(root / 'parsed' / 'bibtex.parquet').to_pylist()
    assert len(bibtex) == 2
    assert {row['doi'] for row in bibtex} == {'10.1234/ref1', '10.1234/ref2'}
    audit = json.loads((root / 'parsed' / 'bibtex_audit.json').read_text(encoding='utf8'))
    mapping = audit['publications']['2411.18799']['references']['result']['merge']['mapping']
    same = [item for item in mapping if item['old_key'] == 'same']
    assert len(same) == 2
    assert len({item['canonical_key'] for item in same}) == 2
    assert all(item['canonical_key'] not in (None, 'same') for item in same)
    elements = pq.read_table(root / 'parsed' / 'elements.parquet').to_pylist()
    shared = [row for row in elements if row['content'] == 'The shared claim is stable.']
    assert len(shared) == 1
    hierarchy = pq.read_table(root / 'parsed' / 'hierarchy.parquet').to_pylist()
    assert {edge['version'] for edge in hierarchy
            if edge['element_id'] == shared[0]['element_id']} == {1, 2}


INVALID_BIB_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
We use \cite{valid} and \cite{empty}.
\begin{thebibliography}{1}
\bibitem{valid} A. Author. Valid Title. 2020.
\bibitem{} Empty key entry. 2021.
\end{thebibliography}
\end{document}
"""


def test_default_services_invalid_key_is_unexportable_not_duplicate(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2411.18799', 1, {
        'main.tex': INVALID_BIB_TEX,
    }, 'succeeded')

    manifest = run_parser(root)

    assert manifest['versions']['versions_parsed'] == 1
    bibtex = pq.read_table(root / 'parsed' / 'bibtex.parquet').to_pylist()
    assert len(bibtex) == 1
    assert bibtex[0]['citation_key'].startswith('ref_')
    assert manifest['bibtex']['before_dedup'] == 2
    assert manifest['bibtex']['after_dedup'] == 1
    assert manifest['bibtex']['deduplicated'] == 0
    assert manifest['quality']['bibtex_unexportable'] == 1
    audit = json.loads((root / 'parsed' / 'bibtex_audit.json').read_text(encoding='utf8'))
    pub = audit['publications']['2411.18799']
    mapping = pub['references']['result']['merge']['mapping']
    valid_rows = [item for item in mapping if item.get('old_key') == 'valid']
    assert len(valid_rows) == 1
    assert valid_rows[0]['canonical_key'].startswith('ref_')
    codes = {f['code'] for f in pub['references']['result']['findings']}
    assert 'invalid_key_unresolved' in codes


def test_source_texts_read_error_downgrades_to_partial(tmp_path, make_source, monkeypatch):
    from parser.pipeline import SourceTextUnavailable

    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {'main.tex': V1}, 'succeeded')

    def boom(*args, **kwargs):
        raise SourceTextUnavailable('main.tex: [Errno 2] No such file')
    monkeypatch.setattr('parser.pipeline._source_texts', boom)

    manifest = run_parser(root)

    assert manifest['versions']['versions_parsed'] == 0
    assert manifest['versions']['versions_partial'] == 1
    assert manifest['versions']['versions_failed'] == 0
    assert any(w['code'] == 'source_texts_unavailable'
               for w in manifest['validation']['warnings'])

    # The failed-to-read version must still be recorded in the audit sidecar.
    audit = json.loads((root / 'parsed' / 'bibtex_audit.json').read_text(encoding='utf8'))
    pub = audit['publications']['2410.23025']
    versions = pub['versions']
    assert len(versions) == 1
    assert versions[0]['version'] == 1
    assert versions[0]['status'] == 'partial'
    assert versions[0]['finalized'] is None
    assert versions[0]['normalized'] is None
    assert versions[0]['bibliography'] is None
    assert versions[0]['reason'] == 'source_texts_unavailable'


def test_selected_bib_override_invalidates_resume(tmp_path, make_source):
    root = tmp_path / 'runtime'
    make_source(root, '2410.23025', 1, {
        'main.tex': V1, 'refs.bib': '@misc{x, title={X}}',
    }, 'succeeded')

    run_parser(root)

    log = []
    run_parser(root, log=log.append)
    assert log and all('skip' in line for line in log), log

    log = []
    run_parser(root, selected_bib_files={('2410.23025', 1): ('refs.bib',)},
               log=log.append)
    assert any('parse 2410.23025' in line for line in log), log


def test_two_version_same_key_different_doi_remaps_content(tmp_path, make_source):
    root = tmp_path / 'runtime'
    for version in (1, 2):
        make_source(root, '2411.18799', version, {
            'main.tex': TWO_VERSION_TEX,
            'refs.bib': (f'@article{{same, title={{Version {version}}}, '
                         f'doi={{10.1234/ref{version}}}}}'),
        }, 'succeeded')

    manifest = run_parser(root)

    assert manifest['versions']['versions_parsed'] == 2
    elements = pq.read_table(root / 'parsed' / 'elements.parquet').to_pylist()
    cite_sentences = [row for row in elements if '\\cite{' in row['content']]
    assert len(cite_sentences) == 2
    keys = {row['content'] for row in cite_sentences}
    assert len(keys) == 2
    audit = json.loads((root / 'parsed' / 'bibtex_audit.json').read_text(encoding='utf8'))
    mapping = audit['publications']['2411.18799']['references']['result']['merge']['mapping']
    canonical = {item['canonical_key'] for item in mapping if item['old_key'] == 'same'}
    assert keys == {f'Version-specific result \\cite{{{key}}}.'
                    for key in canonical}
