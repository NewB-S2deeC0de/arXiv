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
