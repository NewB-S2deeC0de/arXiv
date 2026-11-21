import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .ids import generate_ids
from .progress import ProgressStore


def build_target_folder(start_month: str, start_id: int, end_month: str, end_id: int) -> str:
    """Canonical cross-month-style naming format for all dataset folders."""
    return f"datasets/{start_month}_{start_id:05d}_to_{end_month}_{end_id:05d}"


def normalize_target(target: dict[str, Any]) -> dict[str, Any]:
    """Validate target fields, verify range with generate_ids, and assign canonical folder."""
    required = ("start_month", "start_id", "end_month", "end_id")
    for key in required:
        if key not in target:
            raise ValueError(f"Missing required target field: '{key}'")

    start_month = str(target["start_month"])
    start_id = int(target["start_id"])
    end_month = str(target["end_month"])
    end_id = int(target["end_id"])
    monthly_end_ids = target.get("monthly_end_ids")
    if monthly_end_ids is not None and not isinstance(monthly_end_ids, dict):
        raise ValueError("monthly_end_ids must be a dict mapping month (YYYY-MM) to last serial")

    # generate_ids validates month format, integer bounds, and cross-month completeness
    expected_ids = generate_ids(
        start_month=start_month,
        start_id=start_id,
        end_month=end_month,
        end_id=end_id,
        monthly_end_ids=monthly_end_ids,
    )

    custom_folder = target.get("folder")
    if custom_folder:
        folder_p = Path(custom_folder)
        if folder_p.is_absolute() or ".." in folder_p.parts:
            raise ValueError("Target folder must be a relative path and contained")
        folder = str(folder_p)
    else:
        folder = build_target_folder(start_month, start_id, end_month, end_id)

    return {
        "start_month": start_month,
        "start_id": start_id,
        "end_month": end_month,
        "end_id": end_id,
        "monthly_end_ids": monthly_end_ids,
        "folder": folder,
        "expected_ids": expected_ids,
    }


def mark_target_completed(persistent_root: Path | str, metadata: dict[str, Any] | None = None) -> None:
    """Write lightweight completed.json marker to make future checks instantaneous."""
    root = Path(persistent_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        **(metadata or {}),
    }
    marker = root / "completed.json"
    marker.write_text(json.dumps(payload, indent=2))


def is_target_completed(persistent_root: Path | str, expected_ids: list[str]) -> bool:
    """Check if target has completed. Uses sub-millisecond marker file when available."""
    root = Path(persistent_root)
    marker = root / "completed.json"
    if marker.exists():
        return True

    progress_dir = root / "progress"
    journal = progress_dir / "jobs.journal.jsonl"
    jobs_csv = progress_dir / "jobs.csv"
    if not journal.exists() and not jobs_csv.exists():
        return False

    try:
        store = ProgressStore(root)
    except Exception:
        return False

    if not store.rows:
        return False

    # All expected papers must have completed discovery
    for paper in expected_ids:
        disc_key = (paper, 0, "discovery", 0)
        row = store.rows.get(disc_key)
        if not row or row["status"] not in ("succeeded", "not_found"):
            return False

    # No pending or running jobs
    for key, row in store.rows.items():
        if row["status"] in ("pending", "running"):
            return False

    # Mark completed for instantaneous future checks
    mark_target_completed(root, {"total_jobs": len(store.rows), "expected_papers": len(expected_ids)})
    return True


def clean_scratch_dir(scratch_path: Path | str) -> None:
    """Purge residual files inside scratch directory without deleting the directory itself."""
    path = Path(scratch_path).resolve()
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for item in path.iterdir():
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        except OSError:
            pass
