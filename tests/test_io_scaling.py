import asyncio
import json
from pathlib import Path
import pytest
from crawler.progress import ProgressStore
from crawler.config import CrawlerConfig


def test_attempt_checkpoint_does_not_rewrite_snapshot(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    original=store.path.read_bytes()
    store.start_attempt(('2504.02523',0,'discovery',0))
    assert store.path.read_bytes()==original
    assert ProgressStore(tmp_path).get(('2504.02523',0,'discovery',0))['attempts']==1
    assert (tmp_path/'progress/jobs.journal.jsonl').exists()


def test_journal_torn_tail_and_integrity(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    store.start_attempt(key)
    journal=tmp_path/'progress/jobs.journal.jsonl'
    with journal.open('ab') as f: f.write(b'{"torn":')
    restored=ProgressStore(tmp_path)
    assert restored.get(key)['attempts']==1
    restored.start_attempt(key)
    assert ProgressStore(tmp_path).get(key)['attempts']==2
    data=journal.read_bytes().replace(b'"running"',b'"pending"',1)
    journal.write_bytes(data)
    with pytest.raises(RuntimeError): ProgressStore(tmp_path)


def test_disk_sampling_avoids_repeated_full_walk(tmp_path,monkeypatch):
    from crawler.metrics import MetricsRecorder
    monkeypatch.chdir(tmp_path); Path('runtime').mkdir()
    metrics=MetricsRecorder(CrawlerConfig())
    def forbidden(*args,**kwargs): raise AssertionError('Repeated full disk walk')
    monkeypatch.setattr(Path,'rglob',forbidden)
    metrics.start('run')
    metrics.sample(force_disk=True)
    metrics.stop()

def test_backup_replays_later_journal_records(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    store.start_attempt(key); store.save(with_views=True)
    store.start_attempt(key)
    store.path.write_text('broken primary')
    restored=ProgressStore(tmp_path)
    assert restored.get(key)['attempts']==2
    assert restored.get(key)['total']==2


def test_disk_counter_updates_deletion_and_shared_roots(tmp_path):
    from crawler.disk_usage import DiskUsage
    root=tmp_path/'runtime'; nested=root/'temp'; nested.mkdir(parents=True)
    p=nested/'source'; p.write_bytes(b'1234')
    disk=DiskUsage([root,nested]); assert disk.total==4
    p.write_bytes(b'1234567'); disk.touch(p); assert disk.total==7
    p.unlink(); disk.touch(p); assert disk.total==0


def test_pipeline_reconciles_only_at_startup(tmp_path,monkeypatch):
    import httpx
    from crawler.pipeline import run_pipeline
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2524
    calls=[]; original=ProgressStore.reconcile; clock=[1000.]
    def reconcile(self,*args,**kwargs): calls.append(1); return original(self,*args,**kwargs)
    monkeypatch.setattr(ProgressStore,'reconcile',reconcile)
    async def sleep(n): clock[0]+=n
    def handler(request):
        if request.url.path.startswith('/src/'): return httpx.Response(404)
        ids=request.url.params['id_list'].split(',')
        entries=''.join(f'<entry><id>http://arxiv.org/abs/{i if "v" in i else i+"v1"}</id></entry>' for i in ids)
        return httpx.Response(200,content=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode())
    asyncio.run(run_pipeline(config,{'arxiv'},limit=2,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert len(calls)==1

def test_new_journal_syncs_parent_directory(tmp_path,monkeypatch):
    import os,stat
    from crawler.journal import ProgressJournal
    calls=[]; original=os.fsync
    def sync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode)); return original(fd)
    monkeypatch.setattr(os,'fsync',sync)
    journal=ProgressJournal(tmp_path/'journal.jsonl')
    journal.append([])
    assert True in calls


def test_metadata_absence_can_be_rechecked(tmp_path):
    from crawler.storage import atomic_json
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',1,'metadata',0); store.add(key)
    staging=tmp_path/'stage'; staging.mkdir()
    atomic_json(staging/'normalized.json',{'not_found':True})
    store.storage.commit(key,staging,'not_found'); store.finish(key,'not_found')
    store.reset('not_found'); store.reconcile()
    assert store.get(key)['status']=='pending'

def test_failed_pagination_updates_retained_page_counter(tmp_path,monkeypatch):
    import httpx
    from crawler.pipeline import run_pipeline
    from crawler.metrics import MetricsRecorder
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.execution['allow_anonymous_s2']=True; config.range['end_id']=2523
    store=ProgressStore('runtime'); store.initialize(['2504.02523']); store.add(('2504.02523',1,'references',0)); store.save()
    touched=[]; original=MetricsRecorder.touch; clock=[1000.]
    def touch(self,*paths): touched.extend(Path(p).name for p in paths); return original(self,*paths)
    monkeypatch.setattr(MetricsRecorder,'touch',touch)
    async def sleep(n): clock[0]+=n
    def handler(request):
        return httpx.Response(200,json={'data':[],'next':100}) if request.url.params['offset']=='0' else httpx.Response(404)
    asyncio.run(run_pipeline(config,{'s2'},limit=1,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert 'page-000000.json' in touched and 'page-000000.complete.json' in touched

def test_journal_missing_after_compaction_fails_closed(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    store.start_attempt(key); store.save(with_views=True)
    store.journal.path.unlink()
    with pytest.raises(RuntimeError,match='missing committed records'): ProgressStore(tmp_path)


def test_checkpoint_failure_prevents_http_send(tmp_path,monkeypatch):
    import httpx
    from crawler.scheduler import Scheduler
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0); calls=[]
    def fail(rows): raise OSError('simulated durable write failure')
    monkeypatch.setattr(store.journal,'append',fail)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:calls.append(r))) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client)
            with pytest.raises(OSError): await scheduler.request(key,'arxiv','https://export.arxiv.org/api/query')
    asyncio.run(run())
    assert not calls


def test_legacy_csv_migrates_without_resetting_attempts(tmp_path):
    import csv
    from crawler.storage import atomic_json,digest
    path=tmp_path/'progress/jobs.csv'; path.parent.mkdir()
    rows=[]
    for stage in ('discovery','references'):
        rows.append(dict(paper='2504.02523',version=0,stage=stage,offset=0,status='pending',attempts=1,total=4,cycle=2,error=''))
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=ProgressStore.columns); writer.writeheader(); writer.writerows(rows)
    atomic_json(path.with_suffix('.csv.json'),dict(sha256=digest(path),count=2))
    store=ProgressStore(tmp_path); key=('2504.02523',0,'discovery',0)
    store.start_attempt(key)
    restored=ProgressStore(tmp_path)
    assert restored.get(key)['attempts']==2 and restored.get(key)['total']==5 and restored.get(key)['cycle']==2


def test_missing_key_stops_before_any_http(tmp_path,monkeypatch):
    import httpx
    import crawler.pipeline as pipeline
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline,'load_semantic_scholar_key',lambda:None)
    calls=[]
    async def run():
        with pytest.raises(RuntimeError,match='Set API key'):
            await pipeline.run_pipeline(CrawlerConfig(),{'s2'},limit=10,transport=httpx.MockTransport(lambda r:calls.append(r)))
    asyncio.run(run())
    assert not calls

def test_missing_primary_recovery_keeps_new_attempt_in_journal(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523']); store.save(with_views=True)
    key=('2504.02523',0,'discovery',0)
    store.start_attempt(key)
    store.path.unlink()
    restored=ProgressStore(tmp_path); restored.start_attempt(key)
    restored.path.write_text('corrupted new primary')
    assert ProgressStore(tmp_path).get(key)['attempts']==2


def test_explicit_reload_replays_once(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'discovery',0)
    store.start_attempt(key); store.load()
    assert store.get(key)['attempts']==1
    store.load()
    assert store.get(key)['attempts']==1


def test_session_disk_counter_excludes_untouched_old_files(tmp_path):
    from crawler.disk_usage import DiskUsage
    root=tmp_path/'runtime'; root.mkdir()
    (root/'old').write_bytes(b'x'*1000)
    fresh=root/'fresh'; fresh.write_bytes(b'123')
    disk=DiskUsage([root],scan_existing=False)
    assert disk.total==0
    disk.touch(fresh); assert disk.total==3
    fresh.write_bytes(b'12345'); disk.touch(fresh); assert disk.total==5
    fresh.unlink(); disk.touch(fresh); assert disk.total==0


def test_metrics_scope_and_explicit_scan_are_separate(tmp_path,monkeypatch):
    from crawler.metrics import MetricsRecorder
    monkeypatch.chdir(tmp_path); Path('runtime').mkdir()
    old=Path('runtime/old'); old.write_bytes(b'x'*100000)
    metrics=MetricsRecorder(CrawlerConfig()); metrics.start('scope')
    before=metrics.disk.total
    assert metrics.reconcile_disk()>=100000
    assert metrics.disk.total==before
    summary=metrics.stop()
    assert summary['disk_measurement_scope']=='session_touched_paths'
    assert summary['disk_observed_peak_bytes']<100000
    env=json.loads((metrics.directory/'environment.json').read_text())
    assert env['disk_measurement_scope']=='session_touched_paths'
