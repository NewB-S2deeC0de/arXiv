from pathlib import Path
from typing import Any
from .ids import generate_ids


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
