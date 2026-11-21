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
