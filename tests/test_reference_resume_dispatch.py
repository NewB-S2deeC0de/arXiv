import asyncio
import hashlib
import json

import httpx
import pytest

from crawler.config import CrawlerConfig
from crawler.pipeline import run_pipeline
from crawler.progress import ProgressStore
from crawler.scheduler import Scheduler
from crawler.semantic_scholar import FIELDS
from crawler.reference_worker import query_identity
from crawler.storage import atomic_json, digest


@pytest.mark.parametrize('damage', ['missing', 'checksum', 'invalid_json', 'invalid_page', 'invalid_marker'])
def test_resume_repairs_completed_page_without_resetting_budget(tmp_path, monkeypatch, damage):
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2523
    config.execution['allow_anonymous_s2']=True
    store=ProgressStore('runtime'); paper='2504.02523'
    store.initialize([paper]); base=(paper,1,'references',0); page=(paper,1,'references',100); store.add(base)
    store.start_attempt(base); store.add(page); store.start_attempt(page); store.finish(page,'succeeded')
    fingerprint=query_identity(base[0],1,100)[1]
    directory=store.reference_cache_root(paper,1)/fingerprint
    directory.mkdir(parents=True)
    for offset, data in [(0, {'data':[], 'next':100}), (100, {'data':[], 'next':200})]:
        raw=directory/f'page-{offset:06d}.json'
        raw.write_text(json.dumps(data))
        atomic_json(directory/f'page-{offset:06d}.complete.json',dict(sha256=digest(raw),fingerprint=fingerprint))
    raw=directory/'page-000100.json'; marker=directory/'page-000100.complete.json'
    if damage=='missing': raw.unlink()
    elif damage=='checksum': raw.write_text('changed')
    elif damage=='invalid_marker': marker.write_text('[]')
    else:
        raw.write_text('{' if damage=='invalid_json' else '{"data":[],"next":100}')
        atomic_json(marker,dict(sha256=digest(raw),fingerprint=fingerprint))
    calls=[]; clock=[1000.]
    async def sleep(n): clock[0]+=n
    def handler(request):
        calls.append(int(request.url.params['offset']))
        return httpx.Response(200,json={'data':[]})
    asyncio.run(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    restored=ProgressStore('runtime')
    assert calls==[100]  # Valid page zero is reused; repaired next=None ends the new chain.
    assert restored.get(base)['status']=='succeeded'
    assert (restored.get(page)['attempts'],restored.get(page)['total'],restored.get(page)['cycle'])==(2,2,1)
    before=list(calls)
    asyncio.run(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
    assert calls==before


def test_exhausted_missing_page_does_not_stop_other_papers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2524; config.execution['allow_anonymous_s2']=True
    store=ProgressStore('runtime'); ids=['2504.02523','2504.02524']; store.initialize(ids)
    base=(ids[0],1,'references',0); page=(ids[0],1,'references',100)
    for paper in ids: store.add((paper,1,'references',0))
    store.start_attempt(base); store.add(page)
    for _ in range(50): store.start_attempt(page)
    store.finish(page,'succeeded')
    fingerprint=query_identity(base[0],1,100)[1]
    directory=store.reference_cache_root(ids[0],1)/fingerprint; directory.mkdir(parents=True)
    raw=directory/'page-000000.json'; raw.write_text('{"data":[],"next":100}')
    atomic_json(directory/'page-000000.complete.json',dict(sha256=digest(raw),fingerprint=fingerprint))
    calls=[]
    def handler(request): calls.append(request.url.path); return httpx.Response(200,json={'data':[]})
    asyncio.run(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler)))
    restored=ProgressStore('runtime')
    assert restored.get(base)['status']=='failed'
    assert restored.get(page)['attempts']==50 and restored.get(page)['total']==50
    assert restored.get((ids[1],1,'references',0))['status']=='succeeded'
    assert len(calls)==1 and ids[1] in calls[0]


def test_s2_dispatch_gap_survives_variable_drive_and_log_latency(tmp_path):
    async def run():
        ids=['2504.00001','2504.00002']; store=ProgressStore(tmp_path); store.initialize(ids)
        config=CrawlerConfig(); clock=[1000.]; starts=[]
        async def sleep(n): clock[0]+=n
        def handler(request): starts.append(clock[0]); return httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            persist=scheduler.persist; writes=[0]
            def slow_persist():
                writes[0]+=1
                if writes[0]==1: clock[0]+=10
                persist()
            scheduler.persist=slow_persist
            event=scheduler.event
            def slow_log(name,**fields):
                if name=='request_start' and not starts: clock[0]+=7
                event(name,**fields)
            scheduler.event=slow_log
            for paper in ids:
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
        assert starts[1]-starts[0]>=config.network['s2_key_gap_seconds']-1e-9
        state=json.loads((tmp_path/'progress/services.json').read_text())['s2']
        assert state['recent']==starts
        assert not state['dispatch_pending']
    asyncio.run(run())


def test_pending_dispatch_restart_waits_conservatively(tmp_path):
    async def run():
        store=ProgressStore(tmp_path); store.initialize(['2504.00001'])
        atomic_json(tmp_path/'progress/services.json',dict(
            arxiv=dict(deadline=0,backoff=0,recent=[]),
            s2=dict(deadline=0,backoff=0,recent=[900],dispatch_pending=True)))
        clock=[1000.]; starts=[]
        async def sleep(n): clock[0]+=n
        def handler(r): starts.append(clock[0]); return httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(('2504.00001',0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
        assert starts[0]>=1001.1
    asyncio.run(run())


def test_s2_elapsed_gate_survives_wall_clock_jump(tmp_path):
    async def run():
        ids=['2504.00001','2504.00002']; store=ProgressStore(tmp_path); store.initialize(ids)
        wall=[1000.]; elapsed=[10.]; starts=[]
        async def sleep(n): wall[0]+=n; elapsed[0]+=n
        def handler(r):
            starts.append(elapsed[0])
            if len(starts)==1: wall[0]+=100; elapsed[0]+=.1
            return httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,api_key='test',now=lambda:wall[0],
                                monotonic=lambda:elapsed[0],sleep=sleep)
            for paper in ids:
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
        assert starts[1]-starts[0]>=1.1-1e-9
    asyncio.run(run())
