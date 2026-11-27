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


async def retry_target_failed(
    base_config: CrawlerConfig,
    target: dict[str, Any],
    *,
    force_mark_completed: bool = False,
    takeover_stale: bool = False,
    limit: int | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Retry failed jobs for a target, resetting their attempts and re-running them."""
    from .pipeline import run_pipeline
    pipeline_runner = runner or run_pipeline

    norm = normalize_target(target)
    folder = norm["folder"]
    expected_ids = norm["expected_ids"]
    target_api_key = resolve_s2_key(norm.get("s2_api_key"))
    key_label = f"S2 key: {norm['s2_api_key']}" if norm.get("s2_api_key") else "S2 key: default"
    range_desc = f"{norm['start_month']}.{norm['start_id']:05d} -> {norm['end_month']}.{norm['end_id']:05d}"

    log(f"Retry: target {range_desc} (folder: {folder}, {key_label})")

    root = Path(folder)
    progress_dir = root / "progress"
    if not (progress_dir / "jobs.csv").exists() and not (progress_dir / "jobs.journal.jsonl").exists():
        log(f"Retry: [{folder}] no progress data found. Skipping.")
        return {
            "target": target,
            "folder": folder,
            "failed_before": 0,
            "failed_after": 0,
            "status": "no_progress_data",
        }

    store = ProgressStore(root)
    failed_keys = [k for k, r in store.rows.items() if r["status"] == "failed"]
    failed_before = len(failed_keys)

    if failed_before == 0:
        log(f"Retry: [{folder}] no failed jobs found (failed = 0).")
        if force_mark_completed or is_target_completed(folder, expected_ids):
            mark_target_completed(folder, {"total_jobs": len(store.rows), "expected_papers": len(expected_ids)})
        return {
            "target": target,
            "folder": folder,
            "failed_before": 0,
            "failed_after": 0,
            "status": "no_failed_jobs",
        }

    # Remove existing completed.json marker to permit re-running
    marker = root / "completed.json"
    marker.unlink(missing_ok=True)

    target_config = copy.deepcopy(base_config)
    target_config.range = {
        "start_month": norm["start_month"],
        "start_id": norm["start_id"],
        "end_month": norm["end_month"],
        "end_id": norm["end_id"],
    }
    if norm["monthly_end_ids"]:
        target_config.range["monthly_end_ids"] = norm["monthly_end_ids"]
    target_config.paths["persistent_root"] = folder
    target_config.validate()

    # Step 1: If any discovery jobs failed, reset and run discovery first
    disc_failed = [k for k in failed_keys if k[2] == "discovery"]
    if disc_failed:
        log(f"Retry: [{folder}] resetting {len(disc_failed)} failed discovery jobs...")
        store.reset(status="failed", stages={"discovery"})
        await pipeline_runner(
            target_config,
            {"discovery"},
            resume=True,
            limit=limit,
            takeover_stale=takeover_stale,
            api_key=target_api_key,
        )

    # Step 2: Reset remaining failed jobs for all other stages (metadata, source, references)
    store = ProgressStore(root)
    remaining_to_reset = [k for k, r in store.rows.items() if r["status"] == "failed"]
    if remaining_to_reset:
        log(f"Retry: [{folder}] resetting {len(remaining_to_reset)} failed jobs for stages {{'arxiv', 's2'}}...")
        store.reset(status="failed")

    # Step 3: Run arxiv and s2 if there are pending jobs (from reset or newly discovered)
    store = ProgressStore(root)
    has_active = any(k[2] in ("metadata", "source", "references") and r["status"] in ("pending", "running") for k, r in store.rows.items())
    summary: dict[str, Any] = {}
    if has_active:
        log(f"Retry: [{folder}] running stages {{'arxiv', 's2'}} for active jobs...")
        summary = await pipeline_runner(
            target_config,
            {"arxiv", "s2"},
            resume=True,
            limit=limit,
            takeover_stale=takeover_stale,
            api_key=target_api_key,
        )

    # Step 4: Inspect outcomes after retry
    store = ProgressStore(root)
    failed_after_keys = [k for k, r in store.rows.items() if r["status"] == "failed"]
    failed_after = len(failed_after_keys)

    completed = False
    if failed_after == 0:
        log(f"Retry: [{folder}] all failed jobs recovered successfully (failed = 0)! Marking completed.json.")
        mark_target_completed(folder, summary if isinstance(summary, dict) else {})
        completed = True
    elif force_mark_completed:
        log(f"Retry: [{folder}] FORCE_MARK_COMPLETED=True. Marking completed.json despite {failed_after} remaining failed jobs.")
        mark_target_completed(folder, summary if isinstance(summary, dict) else {})
        completed = True
    else:
        log(f"Retry: [{folder}] still has {failed_after} failed jobs. completed.json NOT created.")

    # Clean scratch SSD directory
    clean_scratch_dir(target_config.paths.get("scratch_root", "scratch"))

    return {
        "target": target,
        "folder": folder,
        "failed_before": failed_before,
        "failed_after": failed_after,
        "remaining_failed_keys": failed_after_keys[:50],
        "completed": completed,
        "status": "completed" if completed else "has_failures",
        "summary": summary,
    }


async def retry_range_queue(
    base_config: CrawlerConfig,
    targets: list[dict[str, Any]],
    *,
    force_mark_completed: bool = False,
    takeover_stale: bool = False,
    limit: int | None = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Retry failed jobs across all specified targets sequentially."""
    results = []
    for t in targets:
        res = await retry_target_failed(
            base_config,
            t,
            force_mark_completed=force_mark_completed,
            takeover_stale=takeover_stale,
            limit=limit,
            runner=runner,
        )
        results.append(res)
    return results

