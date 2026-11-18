import asyncio
import httpx
from crawler.config import CrawlerConfig
from crawler.pipeline import run_pipeline
from crawler.progress import ProgressStore


def test_discovery_stage_runs_only_discovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = CrawlerConfig()
    config.range['start_id'] = 2523
    config.range['end_id'] = 2524  # 2 papers

    urls_called = []

    def handler(request):
        urls_called.append(str(request.url))
        if 'api/query' in str(request.url):
            # Return Atom XML for 2 papers with version 1
            entries = (
                '<entry><id>http://arxiv.org/abs/2504.02523v1</id>'
                '<title>Paper 1</title>'
                '<updated>2025-04-10T00:00:00Z</updated>'
                '<published>2025-04-10T00:00:00Z</published>'
                '</entry>'
                '<entry><id>http://arxiv.org/abs/2504.02524v1</id>'
                '<title>Paper 2</title>'
                '<updated>2025-04-10T00:00:00Z</updated>'
                '<published>2025-04-10T00:00:00Z</published>'
                '</entry>'
            )
            xml = f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
            return httpx.Response(200, content=xml.encode('utf-8'))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    summary = asyncio.run(run_pipeline(config, stages={'discovery'}, limit=2, transport=transport))

    # Verify that only discovery was called
    assert all('api/query' in url for url in urls_called)
    assert not any('/src/' in url for url in urls_called)

    store = ProgressStore(config.paths['persistent_root'])
    counts = store.status_counts()
    assert counts['discovery']['succeeded'] == 2
    # Verify that version rows were generated and are pending
    assert store.get(('2504.02523', 1, 'metadata', 0))['status'] == 'pending'
    assert store.get(('2504.02523', 1, 'source', 0))['status'] == 'pending'
    assert store.get(('2504.02523', 1, 'references', 0))['status'] == 'pending'


def test_discovery_done_sets_when_discoveries_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = CrawlerConfig()
    config.range['start_id'] = 2523
    config.range['end_id'] = 2523  # 1 paper

    def handler(request):
        if 'api/query' in str(request.url):
            xml = (
                '<feed xmlns="http://www.w3.org/2005/Atom">'
                '<entry><id>http://arxiv.org/abs/2504.02523v1</id></entry>'
                '</feed>'
            )
            return httpx.Response(200, content=xml.encode('utf-8'))
        return httpx.Response(404)

    # Pre-populate and complete discovery
    store = ProgressStore(config.paths['persistent_root'])
    store.initialize(['2504.02523'])
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / 'normalized.json').write_text('{"version": 1}')
    (staging / 'response.xml').write_bytes(b'<xml/>')
    (staging / 'request.json').write_text('{}')
    store.storage.commit(('2504.02523', 0, 'discovery', 0), staging)
    store.finish(('2504.02523', 0, 'discovery', 0), 'succeeded')
    store.save(with_views=True)

    # When running with discovery already completed, pipeline finishes cleanly
    transport = httpx.MockTransport(handler)
    summary = asyncio.run(run_pipeline(config, stages={'discovery'}, limit=1, transport=transport))
    assert summary is not None
