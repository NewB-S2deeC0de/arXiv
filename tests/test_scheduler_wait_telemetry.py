import pytest
from unittest.mock import MagicMock
import anyio
from crawler.scheduler import Scheduler
from crawler.config import CrawlerConfig


def test_scheduler_emits_wait_category(tmp_path):
    config = CrawlerConfig()
    store = MagicMock()
    store.root = tmp_path
    client = MagicMock()
    events = []

    def record(name, **fields):
        events.append((name, fields))

    metrics = MagicMock()
    metrics.record.side_effect = record

    now_val = 1000.0

    def mock_now():
        return now_val

    async def mock_sleep(d):
        nonlocal now_val
        now_val += d

    scheduler = Scheduler(config, store, client, now=mock_now, sleep=mock_sleep, metrics=metrics)

    async def run():
        await scheduler.wait(now_val + 0.05, reason="dispatch_pacing", service="arxiv", wait_category="pacing")
        await scheduler.wait(now_val + 0.05, reason="service_cooldown", service="arxiv", wait_category="rate_limit")

    anyio.run(run)

    wait_ends = [e[1] for e in events if e[0] == "wait_end"]
    assert len(wait_ends) == 2
    assert wait_ends[0]["service"] == "arxiv"
    assert wait_ends[0]["wait_category"] == "pacing"
    assert wait_ends[1]["service"] == "arxiv"
    assert wait_ends[1]["wait_category"] == "rate_limit"

from crawler.metrics import MetricsRecorder

def test_metrics_aggregates_api_wait_seconds(tmp_path):
    config = CrawlerConfig()
    config.paths["persistent_root"] = str(tmp_path)
    metrics = MetricsRecorder(config, auth_mode="key")
    metrics.start("run-1")

    metrics.record("wait_end", service="arxiv", wait_category="pacing", seconds=10.5)
    metrics.record("wait_end", service="arxiv", wait_category="rate_limit", seconds=60.0)
    metrics.record("wait_end", service="s2", wait_category="pacing", seconds=5.0)
    metrics.record("wait_end", service="s2", wait_category="rate_limit", seconds=120.0)

    summary = metrics.stop(store=None)
    api_wait = summary["api_wait_seconds"]
    assert api_wait["arxiv"]["pacing_seconds"] == pytest.approx(10.5)
    assert api_wait["arxiv"]["rate_limit_seconds"] == pytest.approx(60.0)
    assert api_wait["s2"]["pacing_seconds"] == pytest.approx(5.0)
    assert api_wait["s2"]["rate_limit_seconds"] == pytest.approx(120.0)
    assert api_wait["total_rate_limit_seconds"] == pytest.approx(180.0)
    assert "effective_crawler_seconds" in api_wait
    assert "total_pacing_seconds" not in api_wait
    assert "total_wait_seconds" not in api_wait

def test_export_and_report_include_api_wait_seconds(tmp_path):
    from crawler.export import _export_snapshot
    from crawler.progress import ProgressStore
    from crawler.storage import atomic_json
    import json

    config = CrawlerConfig()
    config.paths["persistent_root"] = str(tmp_path)
    config.range = {"start_month": "2025-04", "start_id": 1, "end_month": "2025-04", "end_id": 1}

    # Setup 1 paper progress and metrics summary
    store = ProgressStore(tmp_path)
    paper = "2504.00001"
    store.add((paper, 0, "discovery", 0))
    store.finish((paper, 0, "discovery", 0), "succeeded")
    store.add((paper, 1, "source", 0))
    store.finish((paper, 1, "source", 0), "succeeded")
    store.add((paper, 1, "metadata", 0))
    store.finish((paper, 1, "metadata", 0), "succeeded")
    store.save(with_views=True)

    metrics_dir = tmp_path / "metrics" / "run-1"
    metrics_dir.mkdir(parents=True)
    run_summary = {
        "run_id": "run-1",
        "started_at": "2026-11-21T00:00:00Z",
        "finished_at": "2026-11-21T01:00:00Z",
        "wall_seconds": 3600.0,
        "rss_peak_bytes": 1000000,
        "incomplete": False,
        "api_wait_seconds": {
            "arxiv": {"pacing_seconds": 300.0, "rate_limit_seconds": 1200.0},
            "s2": {"pacing_seconds": 100.0, "rate_limit_seconds": 600.0},
            "total_rate_limit_seconds": 1800.0,
            "effective_crawler_seconds": 1800.0,
        }
    }
    atomic_json(metrics_dir / "summary.json", run_summary)

    manifest = _export_snapshot(config, "export-test-1")
    assert "api_wait_seconds" in manifest
    assert manifest["api_wait_seconds"]["total_rate_limit_seconds"] == pytest.approx(1800.0)
    assert manifest["api_wait_seconds"]["arxiv"]["rate_limit_seconds"] == pytest.approx(1200.0)

    # Check run_metrics.json in export target
    export_target = tmp_path / "exports" / "export-test-1"
    run_metrics = json.loads((export_target / "run_metrics.json").read_text())
    assert run_metrics[0]["api_wait_seconds"]["total_rate_limit_seconds"] == pytest.approx(1800.0)


def test_normal_crawl_emits_pacing_not_rate_limit(tmp_path):
    from crawler.progress import ProgressStore
    import httpx
    async def check():
        store = ProgressStore(tmp_path); store.initialize(['2504.02523', '2504.02524'])
        clock = [1000.0]
        async def sleep(n): clock[0] += n
        events = []
        metrics = MagicMock()
        metrics.record.side_effect = lambda name, **fields: events.append((name, fields))

        def handle(request):
            clock[0] += 1.0  # 1.0s download
            return httpx.Response(200, content=b'<feed></feed>')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            scheduler = Scheduler(CrawlerConfig(), store, client, now=lambda: clock[0], monotonic=lambda: clock[0], sleep=sleep, metrics=metrics)
            await scheduler.request(('2504.02523', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query')
            await scheduler.request(('2504.02524', 0, 'discovery', 0), 'arxiv', 'https://export.arxiv.org/api/query')

        wait_ends = [e[1] for e in events if e[0] == 'wait_end']
        # The inter-request spacing must be categorized as pacing, not rate_limit
        assert any(w.get('service') == 'arxiv' and w.get('wait_category') == 'pacing' for w in wait_ends)
        assert not any(w.get('service') == 'arxiv' and w.get('wait_category') == 'rate_limit' for w in wait_ends)
    anyio.run(check)

