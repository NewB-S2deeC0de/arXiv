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
