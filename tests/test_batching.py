import asyncio
import csv
from pathlib import Path
import httpx
import pytest
from crawler.arxiv_client import parse_atom_batch
from crawler.config import CrawlerConfig,load_config
from crawler.progress import ProgressStore
from crawler.scheduler import Scheduler

IDS=['2504.02523','2504.02524']

def feed(ids):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">'+''.join(f'<entry><id>http://arxiv.org/abs/{paper}v1</id><title>T</title></entry>' for paper in ids)+'</feed>').encode()

@pytest.mark.parametrize('raw',[
    b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad_query</id></entry></feed>',
    feed(['2504.09999']),feed([IDS[0],IDS[0]])])
def test_batch_rejects_error_unrequested_duplicate(raw):
    with pytest.raises(ValueError): parse_atom_batch(raw,IDS)

def test_reordered_and_missing():
    assert list(parse_atom_batch(feed(IDS[::-1]),IDS))==IDS
    assert parse_atom_batch(feed(IDS[:1]),IDS)[IDS[1]]['not_found']

def test_default_config_loads():
    assert not Path(load_config(Path('config/crawler.yaml')).paths['scratch_root']).is_absolute()

@pytest.mark.parametrize('status',[429,503])
def test_batch_retry_and_checkpoint(tmp_path,status):
    async def run():
        store=ProgressStore(tmp_path); store.initialize(IDS)
        keys=[(paper,0,'discovery',0) for paper in IDS]
        saves=[]; original=store.save
        def save(*args,**kwargs): saves.append(1); return original(*args,**kwargs)
        store.save=save
        clock=[1000.]; calls=[]
        async def sleep(seconds): clock[0]+=seconds
        def handler(request):
            calls.append(clock[0]); return httpx.Response(status,headers={'Retry-After':'100'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep)
            assert await scheduler.request(keys,'arxiv','https://export.arxiv.org/api/query') is None
            assert len(calls)==3
            assert all(store.get(k)['attempts']==3 and store.get(k)['status']=='failed' for k in keys)
            assert len(saves)==4  # one checkpoint per send plus one terminal checkpoint
            assert scheduler.state['arxiv']['deadline']>=clock[0]+100
        assert [store.get(k)['total'] for k in keys]==[3,3]
    asyncio.run(run())

def test_mixed_batch_rejected_before_mutation(tmp_path):
    async def run():
        store=ProgressStore(tmp_path); store.initialize(IDS)
        keys=[(paper,0,'discovery',0) for paper in IDS]
        store.start_attempt(keys[1]); calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:calls.append(r))) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client)
            with pytest.raises(ValueError,match='same attempt'): await scheduler.request(keys,'arxiv','https://export.arxiv.org/api/query')
        assert not calls
        assert [store.get(k)['attempts'] for k in keys]==[0,1]
    asyncio.run(run())

def test_atomic_attempt_validation(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(IDS)
    keys=[(paper,0,'discovery',0) for paper in IDS]
    for _ in range(3): store.start_attempt(keys[1])
    with pytest.raises(RuntimeError): store.start_attempts(keys)
    assert ProgressStore(tmp_path).get(keys[0])['attempts']==0


def test_pipeline_interleave_raw_and_mixed_budget(tmp_path,monkeypatch):
    from crawler.pipeline import run_pipeline
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2526
    config.network['discovery_batch_size']=2
    all_ids=['2504.02523','2504.02524','2504.02525','2504.02526']
    store=ProgressStore('runtime'); store.initialize(all_ids)
    store.start_attempt((all_ids[-1],0,'discovery',0)); store.start_attempt((all_ids[-1],0,'discovery',0))
    calls=[]; clock=[1000.]
    async def sleep(n): clock[0]+=n
    def handler(request):
        if request.url.path.startswith('/src/'):
            calls.append(('source',request.url.path)); return httpx.Response(404)
        ids=request.url.params['id_list'].split(',')
        discovery=not ids[0].endswith('v1')
        calls.append(('discovery' if discovery else 'metadata',ids))
        return httpx.Response(200,content=feed([i.removesuffix('v1') for i in ids[::-1]]))
    asyncio.run(run_pipeline(config,{'arxiv'},limit=4,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert [c[0] for c in calls[:4]]==['discovery','metadata','discovery','source']
    restored=ProgressStore('runtime')
    assert restored.status_counts()['discovery']=={'succeeded':4}
    assert restored.get((all_ids[-1],0,'discovery',0))['total']==3
    for paper in all_ids:
        key=(paper,0,'discovery',0)
        directory=restored.storage.result_dir(key)
        assert restored.storage.verify(key)
        assert (directory/'response.xml').exists() and (directory/'request.json').exists()
    import json
    summary_dirs=list(Path('runtime/metrics').iterdir())
    with (summary_dirs[0]/'paper_metrics.csv').open() as f:
        rows=list(csv.DictReader(f))
    assert set(r['arxiv_id'] for r in rows)==set(all_ids)


def test_crash_after_first_batch_commit_recovers(tmp_path,monkeypatch):
    from crawler.pipeline import run_pipeline
    from crawler.storage import Storage
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2524
    clock=[1000.]; discovery_calls=[]
    async def sleep(n): clock[0]+=n
    def handler(request):
        if request.url.path.startswith('/src/'): return httpx.Response(404)
        ids=request.url.params['id_list'].split(',')
        if not ids[0].endswith('v1'): discovery_calls.append(ids)
        return httpx.Response(200,content=feed([i.removesuffix('v1') for i in ids]))
    original=Storage.commit
    def interrupted(self,key,staging,status='succeeded'):
        original(self,key,staging,status)
        if key[2]=='discovery': raise RuntimeError('simulated crash after marker')
    monkeypatch.setattr(Storage,'commit',interrupted)
    with pytest.raises(ExceptionGroup):
        asyncio.run(run_pipeline(config,{'arxiv'},limit=2,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    monkeypatch.setattr(Storage,'commit',original)
    asyncio.run(run_pipeline(config,{'arxiv'},limit=2,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert discovery_calls==[IDS,[IDS[1]]]
    store=ProgressStore('runtime')
    assert store.status_counts()['discovery']=={'succeeded':2}
    assert store.get((IDS[0],0,'discovery',0))['attempts']==1
    assert store.get((IDS[1],0,'discovery',0))['attempts']==2

def test_restart_preserves_remaining_batch_budget(tmp_path):
    async def run():
        store=ProgressStore(tmp_path); store.initialize(IDS)
        keys=[(p,0,'discovery',0) for p in IDS]
        store.start_attempts(keys)  # Crash after checkpoint, before receiving response.
        store=ProgressStore(tmp_path); store.reconcile()
        clock=[1000.]; calls=[]
        async def sleep(n): clock[0]+=n
        def handler(request): calls.append(1); return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep)
            assert await scheduler.request(keys,'arxiv','https://export.arxiv.org/api/query') is None
        assert len(calls)==2
        assert [store.get(k)['total'] for k in keys]==[3,3]
    asyncio.run(run())

def test_empty_batch_preserves_evidence_and_resumes(tmp_path,monkeypatch):
    from crawler.pipeline import run_pipeline
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2524
    calls=[]
    def handler(request): calls.append(1); return httpx.Response(200,content=feed([]))
    asyncio.run(run_pipeline(config,{'arxiv'},limit=2,transport=httpx.MockTransport(handler)))
    store=ProgressStore('runtime')
    assert store.status_counts()['discovery']=={'not_found':2}
    for paper in IDS:
        key=(paper,0,'discovery',0)
        assert store.storage.verify(key)['status']=='not_found'
        assert (store.storage.result_dir(key)/'response.xml').read_bytes()==feed([])
    store.reconcile()
    assert store.status_counts()['discovery']=={'not_found':2}
    asyncio.run(run_pipeline(config,{'arxiv'},limit=2,transport=httpx.MockTransport(handler)))
    assert len(calls)==1

def test_version_batch_maps_exact_versions():
    from crawler.arxiv_client import parse_version_batch
    raw=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2504.02523v2</id><updated>2026-01-02</updated></entry><entry><id>http://arxiv.org/abs/2504.02523v1</id><updated>2025-04-03</updated></entry></feed>'
    parsed=parse_version_batch(raw,['2504.02523v1','2504.02523v2'])
    assert parsed['2504.02523v1']['updated']=='2025-04-03'
    assert parsed['2504.02523v2']['updated']=='2026-01-02'
    with pytest.raises(ValueError): parse_version_batch(raw,['2504.02523v1'])

def test_metadata_batch_pipeline_distinct_dates_and_retry(tmp_path,monkeypatch):
    from crawler.pipeline import run_pipeline
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2523
    clock=[1000.]; calls=[]; attempts=[0]
    async def sleep(n): clock[0]+=n
    def handler(request):
        if request.url.path.startswith('/src/'): return httpx.Response(404)
        ids=request.url.params['id_list'].split(','); calls.append(ids)
        if ids==['2504.02523']: return httpx.Response(200,content=feed(['2504.02523']).replace(b'v1</id>',b'v2</id>'))
        attempts[0]+=1
        if attempts[0]==1: return httpx.Response(503)
        entries=''.join(f'<entry><id>http://arxiv.org/abs/{identifier}</id><updated>2025-04-0{identifier[-1]}</updated></entry>' for identifier in ids[::-1])
        return httpx.Response(200,content=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode())
    asyncio.run(run_pipeline(config,{'arxiv'},limit=1,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert calls==[['2504.02523'],['2504.02523v1','2504.02523v2'],['2504.02523v1','2504.02523v2']]
    store=ProgressStore('runtime')
    import json
    for version in (1,2):
        key=('2504.02523',version,'metadata',0)
        assert store.get(key)['attempts']==2
        assert store.storage.verify(key)
        assert json.loads((store.storage.result_dir(key)/'normalized.json').read_text())['updated']==f'2025-04-0{version}'
    before=len(calls)
    asyncio.run(run_pipeline(config,{'arxiv'},limit=1,transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert len(calls)==before

def test_discovery_pipelining(tmp_path, monkeypatch):
    from crawler.pipeline import run_pipeline
    from crawler.storage import Storage
    monkeypatch.chdir(tmp_path)
    config = CrawlerConfig()
    config.range['end_id'] = 2524
    config.network['discovery_batch_size'] = 1
    events = []
    original_commit = Storage.commit
    def delayed_commit(self, key, staging, status='succeeded', *, include=None):
        events.append(('commit_start', key[0]))
        import time as pytime
        pytime.sleep(0.05)
        res = original_commit(self, key, staging, status, include=include)
        events.append(('commit_end', key[0]))
        return res
    monkeypatch.setattr(Storage, 'commit', delayed_commit)

    def handler(request):
        ids = request.url.params['id_list'].split(',')
        events.append(('request', ids[0]))
        return httpx.Response(200, content=feed(ids))

    asyncio.run(run_pipeline(config, {'discovery'}, limit=2, transport=httpx.MockTransport(handler)))
    store = ProgressStore('runtime')
    assert store.status_counts()['discovery'] == {'succeeded': 2}
    for paper in IDS:
        key = (paper, 0, 'discovery', 0)
        assert store.storage.verify(key)
    # Check that both requests were made and both commits succeeded
    req_events = [e for e in events if e[0] == 'request']
    assert len(req_events) == 2
    commit_ends = [e for e in events if e[0] == 'commit_end']
    assert len(commit_ends) == 2
