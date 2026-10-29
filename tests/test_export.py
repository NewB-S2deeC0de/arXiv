from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore
from crawler.storage import atomic_json
from crawler.export import export_snapshot
import pyarrow.parquet as pq

def test_missing_metadata_is_not_exported_as_paper(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    ProgressStore('runtime').initialize(['2504.02523'])
    manifest=export_snapshot(c,'test-run')
    table=pq.read_table('runtime/exports/test-run/papers.parquet')
    assert table.to_pylist()==[]
    assert manifest['papers']==0 and manifest['missing_metadata']==1
    assert (tmp_path/'runtime/exports/test-run/paper_metrics.csv').exists()
    assert (tmp_path/'runtime/exports/test-run/version_metrics.csv').exists()
    assert manifest['input_fingerprint']

def test_export_uses_journal_ref_as_venue_fallback(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523
    store=ProgressStore('runtime'); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    stage=tmp_path/'stage'; stage.mkdir()
    atomic_json(stage/'normalized.json',dict(arxiv_id='2504.02523',version=1,title='T',authors=['A'],published='2024-10-01T00:00:00Z',updated='2024-10-01T00:00:00Z',journal_ref='J. Test 1 (2024)',venue=None))
    store.storage.commit(key,stage,'succeeded'); store.finish(key,'succeeded')
    export_snapshot(c,'test-run')
    rows=pq.read_table('runtime/exports/test-run/papers.parquet').to_pylist()
    assert rows==[dict(arxiv_id='2504.02523',title='T',authors=['A'],submission_date='2024-10-01T00:00:00Z',revised_dates=[],venue='J. Test 1 (2024)')]
