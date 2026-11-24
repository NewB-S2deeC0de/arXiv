"""Execute the notebook's public workflow rather than mirroring its implementation."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from matching.candidate_pool import build_candidate_pool
from matching.labeling import pool_fingerprint
from matching.auto_matcher import write_auto_labels

ROOT = Path(__file__).resolve().parents[1]


def code_cells():
    notebook = json.loads((ROOT / 'notebooks/labeling_tool.ipynb').read_text(encoding='utf-8'))
    assert all(not c.get('outputs') and c.get('execution_count') is None
               for c in notebook['cells'] if c['cell_type'] == 'code')
    return [''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code']


def test_all_six_notebook_cells_run_in_demo_and_save_only_after_a_human_click(monkeypatch):
    pytest.importorskip('ipywidgets')
    import IPython.display
    monkeypatch.setattr(IPython.display, 'display', lambda *args, **kwargs: None)
    monkeypatch.chdir(ROOT)
    codes = code_cells()
    assert len(codes) == 6
    ns = {'__name__': '__notebook__'}
    try:
        for i, code in enumerate(codes):
            exec(compile(code, f'labeling_tool:cell{i + 1}', 'exec'), ns)
            if i == 0:
                ns.update(DEMO=True, ON_COLAB=False, INSTALL_PACKAGES=False)
            if i == 3:
                ui = ns['labeling_ui']
                assert not ns['progress'].exists()
                next(b for b in ui.actions.children
                     if getattr(b, 'description', '') == 'Không có match').click()
                assert len(ui.session.labels) == 1
        assert pq.read_table(ns['progress']).num_rows == 1
        assert ns['labeling_ui'].closed
    finally:
        if 'labeling_ui' in ns:
            ns['labeling_ui'].close()
        if 'demo_directory' in ns:
            ns['demo_directory'].cleanup()


def test_notebook_review_selects_only_pending_auto_keys_and_isolates_run_progress(tmp_path):
    entries = pa.Table.from_pylist([dict(arxiv_id='p', citation_key='k', title='Example',
                                       authors=['Jane Doe'])])
    references = pa.Table.from_pylist([
        dict(arxiv_id='p', ref_arxiv_id=r, ref_title='Example', ref_authors=['Jane Doe'])
        for r in ('r1', 'r2')])
    bib = tmp_path / 'bib.parquet'
    refs = tmp_path / 'refs.parquet'
    pq.write_table(entries, bib)
    pq.write_table(references, refs)
    p = build_candidate_pool(entries, references)
    plan = tmp_path / 'dataset_plan.json'
    plan.write_text(json.dumps({'candidate_pool_sha256': pool_fingerprint(p)}), encoding='utf-8')
    auto = tmp_path / 'auto-run-v2'
    write_auto_labels(p, auto)
    import matching.auto_matcher as matcher_module
    ns = dict(DEMO=False, MODE='review', MEMBER='member1', REVIEWER='hieu',
        BIBTEX_PATH=bib, REFERENCES_PATH=refs, DOI_INDEX=None, PLAN_PATH=plan,
        AUTO_RUN_DIR=auto, PROGRESS_ROOT=tmp_path, json=json, pq=pq, pc=pa.compute,
        build_candidate_pool=build_candidate_pool, pool_fingerprint=pool_fingerprint,
        matcher_module=matcher_module, Path=Path)
    exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)
    assert ns['entry_keys'] == [('p', 'k')]
    assert ns['progress'] == tmp_path / 'review/auto-run-v2/hieu.parquet'
    assert not ns['progress'].exists()


def test_notebook_refuses_to_prepare_manual_on_a_different_snapshot(tmp_path):
    entries = pa.Table.from_pylist([dict(arxiv_id='p', citation_key='k')])
    references = pa.table({'arxiv_id': pa.array([], pa.string()),
                           'ref_arxiv_id': pa.array([], pa.string())})
    bib, refs = tmp_path / 'bib.parquet', tmp_path / 'refs.parquet'
    pq.write_table(entries, bib)
    pq.write_table(references, refs)
    plan = tmp_path / 'dataset_plan.json'
    plan.write_text(json.dumps({'candidate_pool_sha256': 'different'}), encoding='utf-8')
    ns = dict(DEMO=False, MODE='manual', MEMBER='member1', REVIEWER='hieu',
        BIBTEX_PATH=bib, REFERENCES_PATH=refs, DOI_INDEX=None, PLAN_PATH=plan,
        json=json, build_candidate_pool=build_candidate_pool, pool_fingerprint=pool_fingerprint,
        Path=Path)
    with pytest.raises(ValueError, match='Snapshot khác'):
        exec(compile(code_cells()[2], 'labeling_tool:cell3', 'exec'), ns)
