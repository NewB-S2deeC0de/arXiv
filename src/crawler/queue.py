import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .console import log
from .config import CrawlerConfig
from .ids import generate_ids
from .progress import ProgressStore
from .secrets import resolve_s2_key


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

    s2_api_key = target.get("s2_api_key")
    if s2_api_key is not None and not isinstance(s2_api_key, str):
        raise ValueError("s2_api_key must be a string or None")

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
        "s2_api_key": s2_api_key.strip() if s2_api_key and s2_api_key.strip() else None,
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


async def run_range_queue(
    base_config: CrawlerConfig,
    targets: list[dict[str, Any]],
    stages: set[str] = {"discovery", "arxiv", "s2"},
    *,
    takeover_stale: bool = False,
    limit: int | None = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Execute a sequence of crawl targets sequentially, skipping completed ranges."""
    from .pipeline import run_pipeline
    pipeline_runner = runner or run_pipeline

    normalized_targets = [normalize_target(t) for t in targets]
    results = []

    for index, target in enumerate(normalized_targets, start=1):
        folder = target["folder"]
        expected_ids = target["expected_ids"]
        target_api_key = resolve_s2_key(target.get("s2_api_key"))
        key_label = f"S2 key: {target['s2_api_key']}" if target.get("s2_api_key") else "S2 key: default"
        range_desc = f"{target['start_month']}.{target['start_id']:05d} -> {target['end_month']}.{target['end_id']:05d}"
        log(f"Queue: [{index}/{len(normalized_targets)}] target {range_desc} (folder: {folder}, {key_label})")

        if is_target_completed(folder, expected_ids):
            log(f"Queue: target {folder} already completed 100%. Skipping.")
            results.append({
                "target": target,
                "folder": folder,
                "status": "skipped_completed",
            })
            continue

        target_config = copy.deepcopy(base_config)
        target_config.range = {
            "start_month": target["start_month"],
            "start_id": target["start_id"],
            "end_month": target["end_month"],
            "end_id": target["end_id"],
        }
        if target["monthly_end_ids"]:
            target_config.range["monthly_end_ids"] = target["monthly_end_ids"]
        target_config.paths["persistent_root"] = folder
        target_config.validate()

        # Check if discovery already done in folder
        needs_discovery = True
        journal_p = Path(folder) / "progress" / "jobs.journal.jsonl"
        jobs_csv_p = Path(folder) / "progress" / "jobs.csv"
        if journal_p.exists() or jobs_csv_p.exists():
            try:
                store = ProgressStore(folder)
                disc_keys = [(p, 0, "discovery", 0) for p in expected_ids]
                if all(k in store.rows and store.rows[k]["status"] in ("succeeded", "not_found") for k in disc_keys):
                    needs_discovery = False
            except Exception:
                pass

        summary: dict[str, Any] = {}
        if needs_discovery and ("discovery" in stages):
            log(f"Queue: [{folder}] running Stage 1: Discovery...")
            await pipeline_runner(
                target_config,
                {"discovery"},
                resume=True,
                limit=limit,
                takeover_stale=takeover_stale,
                api_key=target_api_key,
            )

        parallel_stages = stages - {"discovery"}
        if parallel_stages:
            log(f"Queue: [{folder}] running Stage 2: {parallel_stages}...")
            summary = await pipeline_runner(
                target_config,
                parallel_stages,
                resume=True,
                limit=limit,
                takeover_stale=takeover_stale,
                api_key=target_api_key,
            )
        else:
            summary = {"status": "discovery_only"}

        completed = is_target_completed(folder, expected_ids)
        if completed:
            mark_target_completed(folder, summary if isinstance(summary, dict) else {})

        if isinstance(summary, dict) and "api_wait_seconds" in summary:
            api_wait = summary.get("api_wait_seconds", {})
            wall = summary.get("wall_seconds", 0.0)
            total_rl = api_wait.get("total_rate_limit_seconds", 0.0)
            ax_rl = api_wait.get("arxiv", {}).get("rate_limit_seconds", 0.0)
            s2_rl = api_wait.get("s2", {}).get("rate_limit_seconds", 0.0)
            effective = api_wait.get("effective_crawler_seconds", max(0.0, wall - total_rl))
            log(
                f"Queue: [{folder}] Wall: {wall:.1f}s | "
                f"Rate-limit stall: {total_rl:.1f}s (arXiv: {ax_rl:.1f}s, S2: {s2_rl:.1f}s) | "
                f"Effective crawl: {effective:.1f}s"
            )

        # Clean scratch SSD directory between targets
        clean_scratch_dir(target_config.paths.get("scratch_root", "scratch"))

        results.append({
            "target": target,
            "folder": folder,
            "status": "completed" if completed else "incomplete",
            "summary": summary,
        })

    log(f"Queue: all {len(normalized_targets)} targets processed.")
    return results
