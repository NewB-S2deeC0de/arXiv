import asyncio
import httpx
from crawler.config import CrawlerConfig
from crawler.pipeline import run_pipeline

def test_broadcast(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.execution['allow_anonymous_s2']=True; c.range['end_id']=2523
    seen=[]
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(404)
    result=asyncio.run(run_pipeline(c,{'arxiv','s2'},False,limit=1,transport=httpx.MockTransport(handler)))
    assert set(seen)=={'export.arxiv.org'}  # No known versions after discovery 404.
    assert result['jobs']==1

def test_reference_page_resume(tmp_path,monkeypatch):
    import json
    from crawler.progress import ProgressStore
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.execution['allow_anonymous_s2']=True; c.range['end_id']=2523
    store=ProgressStore('runtime'); store.initialize(['2504.02523'])
    store.add(('2504.02523',1,'references',0)); store.save()
    calls=[]
    def handler(request):
        offset=int(request.url.params['offset']); calls.append(offset)
        if offset==0: return httpx.Response(200,json={'data':[],'next':100})
        raise RuntimeError('simulated session interruption')
    try: asyncio.run(run_pipeline(c,{'s2'},limit=1,transport=httpx.MockTransport(handler)))
    except ExceptionGroup: pass
    def finish(request):
        calls.append(int(request.url.params['offset']))
        return httpx.Response(200,json={'data':[]})
    asyncio.run(run_pipeline(c,{'s2'},limit=1,transport=httpx.MockTransport(finish)))
    assert calls.count(0)==1
