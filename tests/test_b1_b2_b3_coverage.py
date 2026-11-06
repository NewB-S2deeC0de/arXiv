"""Corpus, corruption and semantic-preservation acceptance tests.

Seeded mutations test invariants, not implementation-specific scanner shapes.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import unittest
import tempfile
from contextlib import redirect_stdout,redirect_stderr
import io

import test_b1_b2_b3_version_workflow as workflow_fixture
from standardization import (normalize_text,normalize_figure,normalize_block_math,
    normalize_a3_hierarchy,scan_latex,scan_citations,remap_citations,read_bibtex,
    prepare_bibtex_entry,SourceContext,make_reference_occurrence,
    analyze_reference_candidates,merge_reference_decisions,prepare_version_bibliography,
    RawSourceMap,SourceMapPiece,MappedSourceSpan,inspect_bibliography_dependencies,
    to_c_tree,remap_a3_hierarchy,inspect_version_bibliography)



def partition(case,raw,parts):
    cursor=0
    for start,end,text in sorted(parts):
        case.assertEqual(start,cursor)
        case.assertEqual(raw[start:end],text)
        cursor=end
    case.assertEqual(cursor,len(raw))


class TokenSemanticsTests(unittest.TestCase):
    def figure(self,body):
        return r'\begin{figure}[t]'+body+r'\caption{Keep \cite{k}}\end{figure}'

    def test_conditional_operands_and_branches_never_cleaned(self):
        for body in (r'\ifx\centering\foo True\else False\fi',
                     r'\iftrue\centering\ifnum1=1\midrule\fi\else\midrule\fi'):
            raw=self.figure(body)
            result=normalize_figure(raw)
            self.assertIn(body,result.content)
            self.assertIn('figure_conditional_preserved',[w.code for w in result.warnings])
            self.assertEqual(normalize_figure(result.content).content,result.content)

    def test_alias_definition_keeps_layout_command_as_operand(self):
        for body in (r'\let\alias\centering',r'\let\centering=\alias',
                     '\\let % comment\n\\alias = % value\n\\midrule',r'\let\alias=X'):
            result=normalize_figure(self.figure(body))
            self.assertIn(body,result.content)

    def test_newif_declaration_does_not_swallow_following_figure(self):
        body=r'\newif\ifdraft\centering\ifdraft Draft\else Final\fi'
        result=normalize_figure(self.figure(body))
        self.assertIn(r'\newif\ifdraft',result.content)
        self.assertNotIn(r'\centering',result.content)
        self.assertIn(r'\ifdraft Draft\else Final\fi',result.content)

    def test_token_primitives_preserve_full_figure(self):
        for name in ('string','meaning','noexpand','expandafter','futurelet','csname','catcode',
                     'afterassignment','aftergroup','the','uppercase','lowercase'):
            raw=self.figure('\\'+name+r'\centering')
            result=normalize_figure(raw)
            self.assertEqual(result.content,raw)
            self.assertIn('figure_cleanup_deferred',[w.code for w in result.warnings])

    def test_incomplete_alias_conditional_and_declaration_keep_raw(self):
        for body in (r'\let{bad}',r'\let\alias{}',r'\newif{bad}',r'\iftrue\centering'):
            raw=self.figure(body)
            result=normalize_figure(raw)
            self.assertEqual(result.content,raw)
            self.assertTrue(result.warnings)

    def test_prose_token_sensitive_commands_do_not_expose_formatting_operands(self):
        for raw in (r'\ifx\textbf{ A}\foo True\else False\fi',
                    r'\let\alias\textbf{ word }',r'\string\emph{ text }',
                    r'\expandafter\textbf{ word }',r'\newif\ifdraft \emph{ word }'):
            result=normalize_text(raw)
            self.assertEqual(result.content,raw)
            self.assertIn('normalization_token_semantics_deferred',[w.code for w in result.warnings])

    def test_primitive_in_nested_formatting_keeps_wrapper_and_raw_semantics(self):
        raw=r'Use \emph{\let\alias\textbf{ word }} now.'
        self.assertEqual(normalize_text(raw).content,raw)

    def test_math_code_url_comments_containing_primitives_remain_protected(self):
        for token in (r'$\string\textbf{x}$',r'\verb|\string\emph{x}|',
                      r'\url{https://example.org/\string}', '% \\string\\textbf{x}\n'):
            raw=r'\emph{Use }'+token
            result=normalize_text(raw)
            self.assertIn(token,result.content)
            self.assertNotIn('normalization_token_semantics_deferred',[w.code for w in result.warnings])

    def test_known_semantic_macros_retain_scientific_notation(self):
        for token in (r'\textsubscript{1}',r'\textsuperscript{3}',r'\ce{NaCl}',r'\si{\micro\metre}'):
            result=normalize_text(r'\textbf{Signal }'+token+'.')
            self.assertEqual(result.content,'Signal '+token+'.')

    def test_raw_figure_faults_fail_closed(self):
        for raw in (r'\begin{figure}$unclosed\end{figure}',r'\begin{figure}{oops\end{figure}',
                    r'\begin{figure}\verb|oops\end{figure}',r'\begin{figure}\caption{X}\end{table}',
                    r'\begin{figure}\begin{verbatim}oops\end{figure}',r'\begin{figure}\caption{X}\end{figure}tail'):
            result=normalize_figure(raw)
            self.assertEqual(result.content,raw)
            self.assertTrue(result.warnings)


class CorruptionInvariantTests(unittest.TestCase):
    def test_seeded_bibtex_corruption_never_loses_input_or_crashes_projection(self):
        source='@article{a,title={Nested {TeX}, title},author={A and {B and C}},year=2024,doi={10.1234/a}}\n@book{b,title="Other",year=2020}'
        randomizer=random.Random(20261105)
        for index in range(240):
            at=randomizer.randrange(len(source)+1)
            operation=index%3
            raw=(source[:at] if operation==0 else source[:at]+source[at+1:] if operation==1
                 else source[:at]+randomizer.choice('{}"%,\\@#\n')+source[at:])
            with self.subTest(index=index):
                parsed=read_bibtex(raw,source=SourceContext('p',1,'refs.bib'))
                partition(self,raw,[(e.start,e.end,e.raw) for e in parsed.entries]+
                          [(f.start,f.end,f.raw) for f in parsed.unparsed])
                self.assertTrue(all(0<=w.start<=w.end<=len(raw) for w in parsed.warnings))
                for entry in parsed.entries:
                    projected=prepare_bibtex_entry(entry)
                    self.assertEqual(projected.entry,entry)

    def test_seeded_mixed_prose_is_idempotent_and_preserves_citation_lexemes(self):
        randomizer=random.Random(7831)
        tokens=['word',r'\textbf{bold}',r'\emph{ nested \textit{word}}',r'\citep[p. 2]{a,b}',
                r'\(x  + y\)',r'\url{https://x.org/a?q=1}',r'\texttt{raw  code}',
                r'\textsubscript{2}',r'\verb|\cite{fake}|']
        for index in range(240):
            raw=' \t '.join(randomizer.choices(tokens,k=6))
            with self.subTest(index=index):
                result=normalize_text(raw)
                self.assertEqual(normalize_text(result.content).content,result.content)
                self.assertEqual(tuple(c.raw for c in scan_citations(raw).citations),
                                 tuple(c.raw for c in scan_citations(result.content).citations))
                scanned=scan_latex(raw)
                partition(self,raw,[(s.start,s.end,s.raw) for s in scanned.segments])

    def test_scanner_truncations_keep_every_character(self):
        raw=r'Text \emph{nested $x$ and \cite{k}} \begin{figure}\caption{X}\end{figure} \verb|a|'
        for end in range(len(raw)+1):
            text=raw[:end];scanned=scan_latex(text)
            partition(self,text,[(s.start,s.end,s.raw) for s in scanned.segments])

    def test_remap_edits_reconstruct_output_and_never_change_non_keys(self):
        raw=r'a \citep[see][p.2]{old, unknown,other} old \verb|\cite{old}| $old$'
        mapping={('p',1,'old'):'new',('p',1,'other'):'final'}
        result=remap_citations(raw,mapping,arxiv_id='p',version=1)
        pieces=[];cursor=0
        for edit in result.edits:
            self.assertEqual(raw[edit.start:edit.end],edit.old_key)
            pieces.extend((raw[cursor:edit.start],edit.new_key));cursor=edit.end
        pieces.append(raw[cursor:])
        self.assertEqual(''.join(pieces),result.content)
        self.assertIn('unknown',result.content)
        self.assertIn(r'\verb|\cite{old}| $old$',result.content)

    def test_bibtex_malformed_quote_comments_and_escape_are_explicit(self):
        for raw in ('@misc{k,title="unclosed}', '@misc(k,title="unclosed)',
                    '@misc{k,title={x} #}', '@misc{k,title="unexpected }"}',
                    '@misc{k,title={x}\n% trailing comment }', '@bad no header'):
            parsed=read_bibtex(raw)
            partition(self,raw,[(e.start,e.end,e.raw) for e in parsed.entries]+
                      [(f.start,f.end,f.raw) for f in parsed.unparsed])
            self.assertTrue(parsed.warnings)

    def test_resource_ambiguity_and_dynamic_paths_never_choose_a_file(self):
        raw=r'\bibliography{refs,\dynamic,../outside,https://example.org/refs}'
        result=inspect_bibliography_dependencies(raw,listed_bib_files=['refs.bib','sub/refs.bib'],root_file='sub/main.tex')
        self.assertTrue(all(t.listed_match is None for t in result.declarations[0].targets))
        self.assertTrue(result.warnings)




class ContractFailureTests(unittest.TestCase):
    def test_unfinalized_invalid_status_and_root_cannot_be_exported(self):
        from types import SimpleNamespace
        normalized=normalize_a3_hierarchy(workflow_fixture.hierarchy()).hierarchy
        for broken in (replace(normalized,status='unknown'),replace(normalized,root=None),
                       replace(normalized,status='unparseable')):
            with self.assertRaises(ValueError):
                to_c_tree(broken,node_factory=SimpleNamespace,structural_content=lambda n:'')
            with self.assertRaises(ValueError):
                remap_a3_hierarchy(broken,{})
        with self.assertRaises(TypeError):
            to_c_tree(normalized,node_factory=SimpleNamespace,structural_content=None)
        with self.assertRaises(TypeError):
            to_c_tree(normalized,node_factory=SimpleNamespace,structural_content=lambda n:None)

    def test_duplicate_occurrences_reject_before_creating_c2_tree_or_remap(self):
        from types import SimpleNamespace
        normalized=normalize_a3_hierarchy(workflow_fixture.hierarchy()).hierarchy
        root=replace(normalized.root,children=normalized.root.children*2)
        broken=replace(normalized,root=root)
        with self.assertRaises(ValueError):
            to_c_tree(broken,node_factory=SimpleNamespace,structural_content=lambda n:'')
        with self.assertRaises(ValueError):
            remap_a3_hierarchy(broken,{})

    def test_stale_bibliography_source_map_keeps_entries_and_diagnostic(self):
        from types import SimpleNamespace as Obj
        raw=r'\begin{thebibliography}{1}\bibitem{k}Reference.\end{thebibliography}'
        hierarchy=Obj(arxiv_id='p',version=1,status='ready',diagnostics=[],
            bibliography=[Obj(raw=raw,source_spans=[MappedSourceSpan('refs.tex',0,len(raw))])])
        gathered=Obj(arxiv_id='p',version=1,status='ready',segments=[],diagnostics=[],bib_files=[],root_file='main.tex')
        result=inspect_version_bibliography(hierarchy,gathered,source_texts={'refs.tex':'stale'})
        self.assertEqual(result.source_mapping_failures,1)
        self.assertEqual(result.blocks[0].bibliography.entries[0].citation_key,'k')
        self.assertIn('bibliography_source_mapping_unavailable',[w.code for w in result.blocks[0].diagnostics])

    def test_invalid_key_never_gets_silent_final_record(self):
        entry=read_bibtex('@misc{bad key,title={Raw retained}}',source=SourceContext('p',1)).entries[0]
        item=make_reference_occurrence(prepare_bibtex_entry(entry),occurrence_id='invalid:0')
        result=merge_reference_decisions([item],{})
        self.assertEqual(result.unresolved,(item.ref,))
        with self.assertRaises(ValueError):
            merge_reference_decisions([item],{item.ref:'final'})
        with self.assertRaises(TypeError):
            merge_reference_decisions([item],None)

    def test_malformed_url_and_incomplete_authors_remain_unknown(self):
        raw='@article{k,title={Keep},doi={https://[invalid},url={https://[invalid},author={A and }}'
        parsed=read_bibtex(raw,source=SourceContext('p',1))
        prepared=prepare_bibtex_entry(parsed.entries[0])
        self.assertIsNone(prepared.payload.doi)
        self.assertIsNone(prepared.payload.url)
        self.assertEqual(prepared.payload.authors,())
        self.assertEqual(prepared.entry.raw,raw)


class BenchmarkBoundaryTests(unittest.TestCase):
    def source(self,folder):
        path=Path(folder)/'source.tex'
        path.write_text(r'\begin{thebibliography}{1}\bibitem{k}Title. DOI: 10.1234/example\end{thebibliography}',encoding='utf-8')
        return path

    def test_real_isolated_worker_cli_generates_valid_report_without_editing_input(self):
        from standardization.benchmark import main
        with tempfile.TemporaryDirectory() as directory:
            source=self.source(directory);before=source.read_bytes()
            output=Path(directory)/'report.json'
            with redirect_stdout(io.StringIO()):
                code=main([str(source),'--output',str(output),'--multipliers','1','1',
                           '--repeats','1','--warmups','1'])
            report=json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(code,0)
            self.assertEqual(len(report['results']),3)
            self.assertEqual({r['stage'] for r in report['results']},{'bibliography','links','pipeline'})
            self.assertTrue(all(r['entries']==1 and r['validation']=='passed' for r in report['results']))
            self.assertEqual(source.read_bytes(),before)

    def test_benchmark_cli_refuses_overwrite_and_invalid_configuration(self):
        from standardization.benchmark import main
        with tempfile.TemporaryDirectory() as directory:
            source=self.source(directory);output=Path(directory)/'existing.json'
            output.write_text('DO NOT OVERWRITE',encoding='utf-8')
            for args in ([str(source),'--output',str(output)], [str(source)],
                         [str(source),'--repeats','0'],[str(source),'--warmups','-1'],
                         [str(source),'--multipliers','0']):
                with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    main(args)
            self.assertEqual(output.read_text(encoding='utf-8'),'DO NOT OVERWRITE')

    def test_worker_mode_outputs_audited_json(self):
        from standardization.benchmark import main
        with tempfile.TemporaryDirectory() as directory:
            source=self.source(directory);stream=io.StringIO()
            with redirect_stdout(stream):
                result=main([str(source),'--worker','links','--multipliers','1','--repeats','1','--warmups','1'])
            self.assertEqual(result,0)
            self.assertEqual(json.loads(stream.getvalue())['validation'],'passed')

    def test_worker_rejects_invalid_stage_and_iteration_counts(self):
        from standardization.benchmark import run_worker
        with tempfile.TemporaryDirectory() as directory:
            source=self.source(directory)
            for stage,repeats,warmups in [('invalid',1,0),('links',0,0),('links',1,-1)]:
                with self.assertRaises(ValueError):
                    run_worker(source,1,stage,repeats,warmups)




if __name__=='__main__':
    unittest.main()
