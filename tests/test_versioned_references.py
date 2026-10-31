import asyncio
import json
from urllib.parse import unquote
from pathlib import Path
import httpx
from crawler.config import CrawlerConfig
from crawler.pipeline import run_pipeline
from crawler.progress import ProgressStore

PAPER='2504.02523'


def setup(tmp_path,monkeypatch,versions=2):
    monkeypatch.chdir(tmp_path)
    config=CrawlerConfig(); config.range['end_id']=2523
    config.execution['allow_anonymous_s2']=True
    store=ProgressStore('runtime'); store.initialize([PAPER])
    for v in range(1,versions+1):
        store.add((PAPER,v,'metadata',0)); store.add((PAPER,v,'source',0))
    store.finish((PAPER,0,'discovery',0),'succeeded'); store.save()
    return config,store


def run(config,handler):
    clock=[1000.]
    async def sleep(n): clock[0]+=n; await asyncio.sleep(0)
    return asyncio.run(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler),
                                   now=lambda:clock[0],sleep=sleep))


def test_versions_have_separate_queries_snapshots_and_legacy_is_preserved(tmp_path,monkeypatch):
    config,store=setup(tmp_path,monkeypatch)
    store.finish((PAPER,0,'references',0),'succeeded')
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json={'data':[]})
    result=run(config,handler)
    assert len(calls)==2 and all('URL:' in unquote(url) for url in calls)
    store=ProgressStore('runtime')
    for v in (1,2):
        key=(PAPER,v,'references',0)
        assert store.get(key)['status']=='succeeded'
        data=json.loads((store.storage.result_dir(key)/'normalized.json').read_text())
        assert data['requested_identifier']==f'URL:https://arxiv.org/abs/{PAPER}v{v}'
        assert data['version_correspondence']=='unverified'
        assert store.storage.verify(key)
    assert store.get((PAPER,0,'references',0))['total']==0
    assert result['status_counts']['references']=={'succeeded':2}
    assert result['status_counts']['legacy_references']=={'succeeded':1}


def test_resume_after_publish_before_progress_ack_does_not_fetch(tmp_path,monkeypatch):
    config,store=setup(tmp_path,monkeypatch,versions=1)
    key=(PAPER,1,'references',0)
    store.add(key); store.start_attempt(key)
    staging=Path('staging'); staging.mkdir()
    (staging/'normalized.json').write_text('{"references":[]}')
    store.storage.commit(key,staging)
    def handler(request): raise AssertionError('published result must be recovered')
    run(config,handler)
    assert ProgressStore('runtime').get(key)['status']=='succeeded'


def test_pagination_uses_next_and_isolated_version_cache(tmp_path,monkeypatch):
    config,store=setup(tmp_path,monkeypatch)
    calls=[]
    def handler(request):
        offset=int(request.url.params['offset'])
        calls.append((request.url.path,offset))
        return httpx.Response(200,json={'data':[],**({'next':230} if offset==0 else {})})
    run(config,handler)
    assert [offset for _,offset in calls]==[0,230,0,230]
    for version in (1,2):
        assert ProgressStore('runtime').get((PAPER,version,'references',230))['status']=='succeeded'


def test_s2_fetch_overlaps_publication_but_queue_is_bounded(tmp_path,monkeypatch):
    import threading
    from crawler.reference_worker import ReferenceWorker
    config,_=setup(tmp_path,monkeypatch,versions=3)
    config.execution['s2_publish_queue_capacity']=1
    entered=threading.Event(); release=threading.Event(); second=asyncio.Event()
    observed=[]; calls=[]
    original=ReferenceWorker.publish_page
    def slow(self,base,*args):
        if base[1]==1:
            entered.set()
            if not release.wait(5): raise AssertionError('producer did not overlap publish')
        return original(self,base,*args)
    monkeypatch.setattr(ReferenceWorker,'publish_page',slow)
    async def scenario():
        clock=[1000.]
        async def sleep(n): clock[0]+=n; await asyncio.sleep(0)
        def handler(request):
            calls.append(request.url.path)
            if len(calls)==2: second.set()
            return httpx.Response(200,json={'data':[]})
        task=asyncio.create_task(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler),now=lambda:clock[0],sleep=sleep))
        try:
            await asyncio.wait_for(second.wait(),3)
            while not entered.is_set(): await asyncio.sleep(.001)
            await asyncio.sleep(.05)
            assert len(calls)==2  # One active page plus one bounded response awaiting admission.
            observed.append(True)
        finally:
            release.set()
        await task
    asyncio.run(scenario())
    assert observed==[True] and len(calls)==3


def test_progress_failure_prevents_dispatch_with_threaded_writer(tmp_path,monkeypatch):
    import pytest
    from crawler.journal import ProgressJournal
    config,_=setup(tmp_path,monkeypatch,versions=1)
    original=ProgressJournal.append
    def fail_attempt(self,rows):
        if any(row['stage']=='references' and row['version']==1 and row['attempts'] for row in rows):
            raise OSError('simulated journal failure')
        return original(self,rows)
    monkeypatch.setattr(ProgressJournal,'append',fail_attempt)
    calls=[]
    def handler(request): calls.append(request); return httpx.Response(200,json={'data':[]})
    with pytest.raises(BaseExceptionGroup): run(config,handler)
    assert not calls


def test_cancel_during_publish_holds_dataset_lock_and_resumes(tmp_path,monkeypatch):
    import threading
    import pytest
    from crawler.storage import Storage
    config,_=setup(tmp_path,monkeypatch,versions=1)
    entered=threading.Event(); release=threading.Event(); calls=[]
    original=Storage.commit
    def slow(self,key,*args,**kwargs):
        if key[2]=='references':
            entered.set(); release.wait(5)
        return original(self,key,*args,**kwargs)
    monkeypatch.setattr(Storage,'commit',slow)
    def handler(request): calls.append(request); return httpx.Response(200,json={'data':[]})
    async def scenario():
        task=asyncio.create_task(run_pipeline(config,{'s2'},transport=httpx.MockTransport(handler)))
        while not entered.is_set(): await asyncio.sleep(.005)
        task.cancel(); await asyncio.sleep(.02); task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        assert Path('runtime/progress/active_run.json').exists()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    monkeypatch.setattr(Storage,'commit',original)
    run(config,handler)
    assert len(calls)==1
