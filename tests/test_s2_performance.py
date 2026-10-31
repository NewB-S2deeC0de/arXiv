import asyncio
import threading
import httpx
from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore
from crawler.scheduler import Scheduler


def test_repeated_429_backoff_bounded_but_retry_after_preserved(tmp_path):
    async def run():
        ids=['2504.00001','2504.00002','2504.00003']
        store=ProgressStore(tmp_path); store.initialize(ids)
        clock=[1000.]; delays=[]
        async def sleep(n): clock[0]+=n
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(429))) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            for paper in ids:
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
                delays.append(scheduler.state['s2']['backoff'])
            assert delays==[240,300,300]
            client._transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'900'}))
            store.reset('failed')
            await scheduler.request((ids[0],0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
            assert scheduler.state['s2']['deadline']>=clock[0]+900
    asyncio.run(run())


def test_s2_latency_counts_towards_gap(tmp_path):
    async def run():
        ids=['2504.00001','2504.00002']; store=ProgressStore(tmp_path); store.initialize(ids)
        clock=[1000.]; starts=[]
        async def sleep(n): clock[0]+=n
        def handler(r): starts.append(clock[0]); clock[0]+=2; return httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            for paper in ids:
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
        assert starts==[1000.,1002.]
    asyncio.run(run())


def test_slow_arxiv_publish_does_not_block_s2(tmp_path,monkeypatch):
    from crawler.pipeline import run_pipeline
    from crawler.storage import Storage
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.range['end_id']=2523; c.execution['allow_anonymous_s2']=True
    store=ProgressStore('runtime'); store.initialize(['2504.02523']); store.add(('2504.02523',1,'references',0)); store.save()
    entered=threading.Event(); s2_seen=threading.Event(); observed=[]
    original=Storage.commit
    def commit(self,key,*args,**kwargs):
        if key[2]=='discovery':
            entered.set(); observed.append(s2_seen.wait(3))
        return original(self,key,*args,**kwargs)
    monkeypatch.setattr(Storage,'commit',commit)
    async def handler(request):
        if request.url.host=='api.semanticscholar.org':
            await asyncio.to_thread(entered.wait,3)
            s2_seen.set(); return httpx.Response(200,json={'data':[]})
        return httpx.Response(200,content=b'<feed xmlns="http://www.w3.org/2005/Atom"/>')
    asyncio.run(run_pipeline(c,transport=httpx.MockTransport(handler)))
    assert observed==[True]


def test_refresh_references_preserves_attempt_history(tmp_path):
    store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
    key=('2504.02523',0,'references',0)
    store.start_attempt(key); store.finish(key,'failed')
    store.refresh('references',{'2504.02523'})
    row=ProgressStore(tmp_path).get(key)
    assert (row['status'],row['attempts'],row['total'],row['cycle'])==('pending',0,1,2)


def test_diagnostics_reads_old_logs_and_torn_tail(tmp_path):
    import json
    from crawler.diagnostics import diagnose_references
    config=CrawlerConfig(); config.paths['persistent_root']=str(tmp_path)
    directory=tmp_path/'logs'/'old'; directory.mkdir(parents=True)
    events=[dict(event='request_end',job=['2504.02523',0,'references',0],status_code=429,seconds=2),
            dict(event='wait_end',job=['2504.02523',0,'references',0],seconds=60)]
    (directory/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events)+'{"torn":')
    result=diagnose_references(config)['runs'][0]
    assert result['http_status_counts']=={'429':1}
    assert result['wait_seconds']==60
