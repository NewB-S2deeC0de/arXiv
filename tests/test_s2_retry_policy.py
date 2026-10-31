import asyncio
import json
import httpx
import pytest
from crawler.config import CrawlerConfig
from crawler.progress import ProgressStore
from crawler.scheduler import Scheduler
from crawler.storage import atomic_json

PAPER='2504.02523'
KEY=(PAPER,1,'references',0)
URL='https://api.semanticscholar.org/graph/v1/paper/test/references'


def test_fifty_429_attempts_only_use_pacing(tmp_path):
    async def run():
        config=CrawlerConfig(); clock=[1000.]; starts=[]
        store=ProgressStore(tmp_path); store.initialize([PAPER]); store.add(KEY); store.save()
        async def sleep(n): clock[0]+=n
        def handler(request): starts.append(clock[0]); return httpx.Response(429)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            assert await scheduler.request(KEY,'s2',URL) is None
        assert len(starts)==50
        assert all(abs(b-a-1.1)<1e-8 for a,b in zip(starts,starts[1:]))
        assert store.get(KEY)['status']=='failed' and store.get(KEY)['attempts']==50
        assert scheduler.state['s2']['backoff']==0
    asyncio.run(run())


def test_retry_after_wins_and_survives_restart(tmp_path):
    async def run():
        config=CrawlerConfig(); config.network['s2_max_attempts']=1
        clock=[1000.]; starts=[]
        store=ProgressStore(tmp_path); store.initialize([PAPER]); store.add(KEY); store.save()
        async def sleep(n): clock[0]+=n
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'900'}))) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(KEY,'s2',URL)
            assert scheduler.state['s2']['deadline']==1900
        config.network['s2_max_attempts']=50
        restored=ProgressStore(tmp_path); restored.reset('failed')
        def handler(request): starts.append(clock[0]); return httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,restored,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(KEY,'s2',URL)
        assert starts==[1900]
    asyncio.run(run())


def test_resume_preserves_49_attempts_and_arxiv_budget(tmp_path):
    store=ProgressStore(tmp_path,s2_max_attempts=50); store.initialize([PAPER]); store.add(KEY)
    for _ in range(49): store.start_attempt(KEY)
    restored=ProgressStore(tmp_path,s2_max_attempts=50); restored.reconcile(recovery_only=True)
    assert restored.get(KEY)['status']=='pending'
    restored.start_attempt(KEY)
    with pytest.raises(RuntimeError): restored.start_attempt(KEY)
    arxiv=(PAPER,0,'discovery',0)
    for _ in range(3): restored.start_attempt(arxiv)
    with pytest.raises(RuntimeError): restored.start_attempt(arxiv)
    assert restored.get(KEY)['total']==50


def test_short_retry_after_does_not_get_generic_retry_delay(tmp_path):
    async def run():
        config=CrawlerConfig(); clock=[1000.]; starts=[]
        store=ProgressStore(tmp_path); store.initialize([PAPER]); store.add(KEY); store.save()
        async def sleep(n): clock[0]+=n
        def handler(request):
            starts.append(clock[0])
            return httpx.Response(429,headers={'Retry-After':'2'}) if len(starts)==1 else httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(KEY,'s2',URL)
        assert starts==[1000,1002]
    asyncio.run(run())


def test_legacy_deadline_is_preserved_once_without_restarting_backoff(tmp_path):
    async def run():
        config=CrawlerConfig(); clock=[1000.]; starts=[]
        store=ProgressStore(tmp_path); store.initialize([PAPER]); store.add(KEY); store.save()
        atomic_json(tmp_path/'progress/services.json',dict(
            arxiv=dict(deadline=0,backoff=0,recent=[]),s2=dict(deadline=1300,backoff=300,recent=[])))
        async def sleep(n): clock[0]+=n
        def handler(request):
            starts.append(clock[0])
            return httpx.Response(429) if len(starts)==1 else httpx.Response(200,json={'data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(KEY,'s2',URL)
        assert starts==pytest.approx([1300,1301.1])
        state=json.loads((tmp_path/'progress/services.json').read_text())['s2']
        assert state['legacy_deadline']==1300 and state['backoff']==0
    asyncio.run(run())


def test_other_temporary_errors_can_retry_past_third_attempt(tmp_path):
    async def run():
        config=CrawlerConfig(); config.network['s2_max_attempts']=5
        clock=[1000.]; starts=[]
        store=ProgressStore(tmp_path); store.initialize([PAPER]); store.add(KEY); store.save()
        async def sleep(n): clock[0]+=n
        def handler(request): starts.append(clock[0]); return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler=Scheduler(config,store,client,api_key='test',now=lambda:clock[0],sleep=sleep)
            await scheduler.request(KEY,'s2',URL)
        assert starts==[1000,1010,1040,1070,1100]
        assert store.get(KEY)['attempts']==5 and store.get(KEY)['error']=='http_503'
    asyncio.run(run())


@pytest.mark.parametrize('value',[0,-1,True,1.5])
def test_invalid_attempt_budget(value):
    config=CrawlerConfig(); config.network['s2_max_attempts']=value
    with pytest.raises(ValueError,match='s2_max_attempts'): config.validate()
