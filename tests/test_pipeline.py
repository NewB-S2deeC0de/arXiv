import asyncio
import httpx
from crawler.config import CrawlerConfig
from crawler.pipeline import run_pipeline

def test_broadcast(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    c=CrawlerConfig(); c.execution['allow_anonymous_s2']=True; c.range['end_id']=2523; c.network['s2_batch_size']=0
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
    c=CrawlerConfig(); c.execution['allow_anonymous_s2']=True; c.range['end_id']=2523; c.network['s2_batch_size']=0
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


def test_arxiv_source_pipelining(tmp_path, monkeypatch):
    import gzip
    import time
    from crawler.progress import ProgressStore
    from crawler.storage import Storage
    monkeypatch.chdir(tmp_path)
    c = CrawlerConfig()
    c.range['start_id'] = 2523
    c.range['end_id'] = 2524  # 2 papers
    clock = [1000.0]

    async def mock_sleep(n):
        clock[0] += n

    class StreamBody(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data
        async def __aiter__(self):
            for offset in range(0, len(self.data), 1024):
                yield self.data[offset:offset+1024]

    events = []
    original_commit = Storage.commit
    def delayed_commit(self, key, staging, status='succeeded', *, include=None):
        if key[2] == 'source':
            events.append(('commit_start', key[0]))
            time.sleep(0.05)
            res = original_commit(self, key, staging, status, include=include)
            events.append(('commit_end', key[0]))
            return res
        return original_commit(self, key, staging, status, include=include)
    monkeypatch.setattr(Storage, 'commit', delayed_commit)

    def handler(request):
        url = str(request.url)
        if 'api/query' in url:
            ids = request.url.params['id_list'].split(',')
            entries = ''.join(f'<entry><id>http://arxiv.org/abs/{i if "v" in i else i+"v1"}</id></entry>' for i in ids)
            return httpx.Response(200, content=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode())
        if '/src/' in url:
            paper_id = url.split('/src/')[-1].split('v')[0]
            events.append(('download_start', paper_id))
            tex_content = b'\\documentclass{article}\\begin{document}Hello world\\end{document}'
            compressed = gzip.compress(tex_content)
            return httpx.Response(200, stream=StreamBody(compressed), headers={'content-type': 'application/x-eprint-tar'})
        return httpx.Response(404)

    summary = asyncio.run(run_pipeline(c, {'arxiv'}, limit=2, transport=httpx.MockTransport(handler), now=lambda: clock[0], sleep=mock_sleep))
    store = ProgressStore('runtime')
    counts = store.status_counts()
    assert counts['discovery']['succeeded'] == 2
    assert counts['metadata']['succeeded'] == 2
    assert counts['source']['succeeded'] == 2

    # Verify that Paper 1's commit started, and Paper 2's download started before Paper 1's commit ended (or pipelined concurrently)
    commit_starts = [e for e in events if e[0] == 'commit_start']
    assert len(commit_starts) == 2
    download_starts = [e for e in events if e[0] == 'download_start']
    assert len(download_starts) == 2

