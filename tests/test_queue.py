import pytest
from crawler.queue import build_target_folder, normalize_target

def test_build_target_folder_same_month():
    assert build_target_folder("2025-04", 2523, "2025-04", 7522) == "datasets/2025-04_02523_to_2025-04_07522"

def test_build_target_folder_cross_month():
    assert build_target_folder("2025-04", 20000, "2025-05", 2500) == "datasets/2025-04_20000_to_2025-05_02500"

def test_normalize_target_auto_folder():
    target = {"start_month": "2025-04", "start_id": 2523, "end_month": "2025-04", "end_id": 7522}
    normalized = normalize_target(target)
    assert normalized["folder"] == "datasets/2025-04_02523_to_2025-04_07522"
    assert len(normalized["expected_ids"]) == 5000

def test_normalize_target_cross_month_valid():
    target = {
        "start_month": "2025-04",
        "start_id": 25000,
        "end_month": "2025-05",
        "end_id": 100,
        "monthly_end_ids": {"2025-04": 25050},
    }
    normalized = normalize_target(target)
    assert normalized["folder"] == "datasets/2025-04_25000_to_2025-05_00100"
    assert len(normalized["expected_ids"]) == 151  # (25050 - 25000 + 1) + 100

def test_normalize_target_cross_month_missing_bounds_raises():
    target = {"start_month": "2025-04", "start_id": 25000, "end_month": "2025-05", "end_id": 100}
    with pytest.raises(ValueError, match="Missing monthly_end_ids"):
        normalize_target(target)

def test_normalize_target_custom_folder_preserved():
    target = {
        "start_month": "2025-04",
        "start_id": 2523,
        "end_month": "2025-04",
        "end_id": 7522,
        "folder": "datasets/my_custom_run",
    }
    normalized = normalize_target(target)
    assert normalized["folder"] == "datasets/my_custom_run"

def test_normalize_target_invalid_keys_or_bounds():
    with pytest.raises(ValueError, match="Missing required target field"):
        normalize_target({"start_month": "2025-04"})
    with pytest.raises(ValueError, match="Invalid inclusive ID range"):
        normalize_target({"start_month": "2025-04", "start_id": 5000, "end_month": "2025-04", "end_id": 1000})

from pathlib import Path
from crawler.queue import is_target_completed, mark_target_completed, clean_scratch_dir
from crawler.progress import ProgressStore

def test_is_target_completed_empty(tmp_path):
    assert not is_target_completed(tmp_path, ["2504.00001"])

def test_is_target_completed_via_marker(tmp_path):
    assert not is_target_completed(tmp_path, ["2504.00001"])
    mark_target_completed(tmp_path, {"total_papers": 1})
    assert (tmp_path / "completed.json").exists()
    assert is_target_completed(tmp_path, ["2504.00001"])

def test_is_target_completed_when_all_finished_without_marker(tmp_path):
    store = ProgressStore(tmp_path)
    store.add(("2504.00001", 0, "discovery", 0))
    store.finish(("2504.00001", 0, "discovery", 0), "succeeded")
    store.add(("2504.00001", 1, "source", 0))
    store.finish(("2504.00001", 1, "source", 0), "succeeded")
    store.add(("2504.00001", 1, "metadata", 0))
    store.finish(("2504.00001", 1, "metadata", 0), "succeeded")
    store.add(("2504.00001", 1, "references", 0))
    store.finish(("2504.00001", 1, "references", 0), "succeeded")
    store.save(with_views=True)
    assert is_target_completed(tmp_path, ["2504.00001"])
    # Should have written completed.json marker
    assert (tmp_path / "completed.json").exists()

def test_is_target_completed_false_if_pending_jobs(tmp_path):
    store = ProgressStore(tmp_path)
    store.add(("2504.00001", 0, "discovery", 0))
    store.finish(("2504.00001", 0, "discovery", 0), "succeeded")
    store.add(("2504.00001", 1, "source", 0))
    store.save(with_views=True)
    assert not is_target_completed(tmp_path, ["2504.00001"])

def test_clean_scratch_dir(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "temp.download").write_bytes(b"temp")
    sub = scratch / "subfolder"
    sub.mkdir()
    (sub / "file.txt").write_text("hello")
    clean_scratch_dir(scratch)
    assert scratch.exists()
    assert list(scratch.iterdir()) == []

from unittest.mock import AsyncMock
from crawler.config import CrawlerConfig
from crawler.queue import run_range_queue

@pytest.mark.anyio
async def test_run_range_queue_executes_sequentially(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_runner = AsyncMock(return_value={"wall_seconds": 10.0, "status_counts": {}})
    config = CrawlerConfig()
    t1_folder = "datasets/t1"
    t2_folder = "datasets/t2"
    targets = [
        {"start_month": "2025-04", "start_id": 1, "end_month": "2025-04", "end_id": 5, "folder": t1_folder},
        {"start_month": "2025-04", "start_id": 6, "end_month": "2025-04", "end_id": 10, "folder": t2_folder},
    ]

    results = await run_range_queue(config, targets, runner=mock_runner)
    assert len(results) == 2
    assert results[0]["folder"] == t1_folder
    assert results[1]["folder"] == t2_folder
    # mock_runner should have been called for t1 (discovery, arxiv+s2) and t2 (discovery, arxiv+s2)
    assert mock_runner.call_count >= 2
    # Verify configs passed to runner
    first_config = mock_runner.call_args_list[0][0][0]
    assert first_config.paths["persistent_root"] == t1_folder

@pytest.mark.anyio
async def test_run_range_queue_skips_already_completed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_runner = AsyncMock(return_value={})
    config = CrawlerConfig()
    t1_folder = "datasets/t1"
    mark_target_completed(t1_folder, {"done": True})
    targets = [
        {"start_month": "2025-04", "start_id": 1, "end_month": "2025-04", "end_id": 5, "folder": t1_folder},
        {"start_month": "2025-04", "start_id": 6, "end_month": "2025-04", "end_id": 10, "folder": "datasets/t2"},
    ]

    results = await run_range_queue(config, targets, runner=mock_runner)
    assert len(results) == 2
    assert results[0]["status"] == "skipped_completed"
    # mock_runner should only be called for t2
    for call in mock_runner.call_args_list:
        cfg = call[0][0]
        assert cfg.paths["persistent_root"] != t1_folder

@pytest.mark.anyio
async def test_run_range_queue_logs_api_wait_telemetry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary_with_wait = {
        "wall_seconds": 120.0,
        "api_wait_seconds": {
            "arxiv": {"pacing_seconds": 10.0, "rate_limit_seconds": 30.0},
            "s2": {"pacing_seconds": 5.0, "rate_limit_seconds": 20.0},
            "total_rate_limit_seconds": 50.0,
            "effective_crawler_seconds": 70.0,
        }
    }
    mock_runner = AsyncMock(return_value=summary_with_wait)
    config = CrawlerConfig()
    target = {"start_month": "2025-04", "start_id": 1, "end_month": "2025-04", "end_id": 2, "folder": "datasets/t1"}
    
    logs = []
    monkeypatch.setattr("crawler.queue.log", lambda msg: logs.append(msg))
    results = await run_range_queue(config, [target], runner=mock_runner)
    assert len(results) == 1
    assert any("Rate-limit stall: 50.0s" in m and "Effective crawl: 70.0s" in m for m in logs)

def test_normalize_target_s2_api_key():
    target = {
        "start_month": "2025-04",
        "start_id": 1,
        "end_month": "2025-04",
        "end_id": 5,
        "s2_api_key": "  my-secret-key-123  ",
    }
    norm = normalize_target(target)
    assert norm["s2_api_key"] == "my-secret-key-123"

    with pytest.raises(ValueError, match="s2_api_key must be a string or None"):
        normalize_target({
            "start_month": "2025-04",
            "start_id": 1,
            "end_month": "2025-04",
            "end_id": 5,
            "s2_api_key": 12345,
        })

@pytest.mark.anyio
async def test_run_range_queue_passes_target_api_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_runner = AsyncMock(return_value={"wall_seconds": 10.0})
    config = CrawlerConfig()
    target = {
        "start_month": "2025-04",
        "start_id": 1,
        "end_month": "2025-04",
        "end_id": 2,
        "folder": "datasets/t1",
        "s2_api_key": "member-custom-key",
    }
    await run_range_queue(config, [target], stages={"arxiv", "s2"}, runner=mock_runner)
    # Check that mock_runner was called with api_key="member-custom-key"
    called_api_keys = [c.kwargs.get("api_key") for c in mock_runner.call_args_list]
    assert "member-custom-key" in called_api_keys


