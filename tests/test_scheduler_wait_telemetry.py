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
