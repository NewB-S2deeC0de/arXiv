import asyncio
import json
import httpx
from crawler.scheduler import Scheduler
from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore

def test_last_429_persists(tmp_path):
    async def check():
        store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
        key=('2504.02523',0,'discovery',0)
        clock=[1000.0]
        async def sleep(n): clock[0]+=n
        transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'100'}))
        async with httpx.AsyncClient(transport=transport) as client:
            s=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep)
            result=await s.request(key,'arxiv','https://export.arxiv.org/api/query')
            assert result is None
            assert store.get(key)['attempts']==3
            assert s.state['arxiv']['deadline']>=clock[0]+100
            other=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep)
            assert other.state==s.state
    asyncio.run(check())

def test_redirect_is_paced_and_key_does_not_leak(tmp_path):
    async def check():
        store=ProgressStore(tmp_path); store.initialize(['2504.02523'])
        key=('2504.02523',0,'discovery',0); clock=[1000.0]; calls=[]
        async def sleep(n): clock[0]+=n
        def handle(request):
            calls.append((clock[0],request.headers.get('x-api-key')))
            return httpx.Response(302,headers={'Location':'/api/next'}) if len(calls)==1 else httpx.Response(200,content=b'ok')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep,api_key='secret')
            assert await scheduler.request(key,'arxiv','https://export.arxiv.org/api/query')==b'ok'
            assert calls[1][0]-calls[0][0]>=3
            assert all(key is None for _,key in calls)
    asyncio.run(check())

def test_retry_after_date_and_long_wait():
    from datetime import datetime,timezone
    from crawler.retry import parse_retry_after
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    assert parse_retry_after('Fri, 02 Jan 2026 00:00:00 GMT',now)==86400
    assert parse_retry_after('90000',now)==90000

def test_auth_stops_service_and_anonymous_window(tmp_path):
    async def check():
        store=ProgressStore(tmp_path); store.initialize(['2504.02523','2504.02524'])
        c=CrawlerConfig(); c.network['s2_anonymous_window_requests']=1
        clock=[1000.]; calls=[]
        async def sleep(n): clock[0]+=n
        def handler(request):
            calls.append(clock[0]); return httpx.Response(200,content=b'ok')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(c,store,client,now=lambda:clock[0],sleep=sleep)
            for paper in ('2504.02523','2504.02524'):
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
            assert calls[1]-calls[0]>=300
        other=ProgressStore(tmp_path/'auth'); other.initialize(['2504.02523','2504.02524'])
        calls.clear()
        def denied(request): calls.append(1); return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
            scheduler=Scheduler(c,other,client,now=lambda:clock[0],sleep=sleep)
            for paper in ('2504.02523','2504.02524'):
                await scheduler.request((paper,0,'references',0),'s2','https://api.semanticscholar.org/graph/v1/paper/test/references')
            assert len(calls)==1 and 's2' in scheduler.stopped
    asyncio.run(check())

def test_s2_batch_post_retries_transport_failure_with_same_body(tmp_path):
    async def check():
        store=ProgressStore(tmp_path); store.initialize(['2504.02523','2504.02524'])
        keys=[]
        for paper in ('2504.02523','2504.02524'):
            key=(paper,1,'references',0); store.add(key); keys.append(key)
        store.save(); calls=[]; clock=[1000.]
        async def sleep(seconds): clock[0]+=seconds
        def handler(request):
            calls.append((request.method,json.loads(request.content)))
            if len(calls)==1: raise httpx.ReadError('temporary',request=request)
            return httpx.Response(200,content=b'[]')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,api_key='key',now=lambda:clock[0],sleep=sleep)
            result=await scheduler.request(keys,'s2','https://api.semanticscholar.org/graph/v1/paper/batch',
                method='POST',json_body={'ids':['URL:v1','URL:v2']})
        assert result==b'[]'
        assert calls==[('POST',{'ids':['URL:v1','URL:v2']})]*2
    asyncio.run(check())

def test_arxiv_dispatch_pacing(tmp_path):
    import pytest
    async def check():
        store=ProgressStore(tmp_path); store.initialize(['2504.02523','2504.02524'])
        clock=[1000.0]; dispatch_times=[]
        async def sleep(n): clock[0]+=n
        def handle(request):
            dispatch_times.append(clock[0])
            clock[0]+=1.5
            return httpx.Response(200,content=b'<feed></feed>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            scheduler=Scheduler(CrawlerConfig(),store,client,now=lambda:clock[0],sleep=sleep)
            await scheduler.request(('2504.02523',0,'discovery',0),'arxiv','https://export.arxiv.org/api/query')
            await scheduler.request(('2504.02524',0,'discovery',0),'arxiv','https://export.arxiv.org/api/query')
        assert len(dispatch_times)==2
        assert dispatch_times[1]-1000.0==pytest.approx(3.0)
    asyncio.run(check())


def test_arxiv_429_backoff_capped_at_60s(tmp_path):
    async def check():
        store = ProgressStore(tmp_path); store.initialize(['2504.02523'])
        key = ('2504.02523', 0, 'discovery', 0)
        clock = [1000.0]
        async def sleep(n): clock[0] += n
        transport = httpx.MockTransport(lambda r: httpx.Response(429))
        async with httpx.AsyncClient(transport=transport) as client:
            s = Scheduler(CrawlerConfig(), store, client, now=lambda: clock[0], sleep=sleep)
            result = await s.request(key, 'arxiv', 'https://export.arxiv.org/api/query')
            assert result is None
            assert s.state['arxiv']['backoff'] == 60
    asyncio.run(check())


def test_arxiv_validation_overlaps_pacing(tmp_path):
    import pytest
    async def check():
        store = ProgressStore(tmp_path); store.initialize(['2504.02523', '2504.02524'])
        clock = [1000.0]; dispatch_times = []
        async def sleep(n): clock[0] += n
        def handle(request):
            dispatch_times.append(clock[0])
            clock[0] += 0.5  # download takes 0.5s
            return httpx.Response(200, content=b'<feed></feed>')
        def slow_validate(raw):
            clock[0] += 1.0  # validate takes 1.0s of CPU time
            return raw
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            scheduler = Scheduler(CrawlerConfig(), store, client, now=lambda: clock[0], sleep=sleep)
            await scheduler.request(('2504.02523', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query', validate=slow_validate)
            await scheduler.request(('2504.02524', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query')
        assert len(dispatch_times) == 2
        # Request 1 started at 1000.0, download finished at 1000.5.
        # With overlapping pacing, next dispatch is max(1000.0 + 3.0, 1000.5) = 1003.0.
        assert dispatch_times[1] == pytest.approx(1003.0)
    asyncio.run(check())


def test_arxiv_slow_download_dispatches_immediately(tmp_path):
    import pytest
    async def check():
        store = ProgressStore(tmp_path); store.initialize(['2504.02523', '2504.02524'])
        clock = [1000.0]; dispatch_times = []
        async def sleep(n): clock[0] += n
        def handle(request):
            dispatch_times.append(clock[0])
            clock[0] += 4.0  # download takes 4.0s (> 3.0s gap)
            return httpx.Response(200, content=b'<feed></feed>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            scheduler = Scheduler(CrawlerConfig(), store, client, now=lambda: clock[0], monotonic=lambda: clock[0], sleep=sleep)
            await scheduler.request(('2504.02523', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query')
            await scheduler.request(('2504.02524', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query')
        assert len(dispatch_times) == 2
        # Request 1 started at 1000.0, download finished at 1004.0.
        # Since 1004.0 >= 1000.0 + 3.0, next request dispatches immediately at 1004.0.
        assert dispatch_times[1] == pytest.approx(1004.0)
    asyncio.run(check())



