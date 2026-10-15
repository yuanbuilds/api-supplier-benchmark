"""Build the standard, date-grouped locations for benchmark runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from workbench.config import ROOT


RUN_KINDS = frozenset({"chat", "image", "video", "business", "suite"})


def write_engine_handoff(path: str | Path | None, run_dir: Path) -> None:
    """Hand an engine-created run directory to its public runner without parsing stdout."""
    if path is not None:
        Path(path).write_text(json.dumps({"run_dir": str(run_dir)}, ensure_ascii=False) + "\n", encoding="utf-8")


def read_engine_handoff(path: str | Path) -> Path:
    """Read and validate the run directory returned by an engine process."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"engine result handoff is missing or invalid: {error}") from error
    run_dir_value = payload.get("run_dir") if isinstance(payload, dict) else None
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise ValueError("engine result handoff requires a non-empty run_dir")
    run_dir = Path(run_dir_value)
    if not run_dir.is_dir():
        raise ValueError(f"engine run_dir does not exist: {run_dir}")
    return run_dir


def new_run_id(name: str, *, now: datetime | None = None) -> str:
    """Return a unique run name; the date belongs to its parent directory."""
    timestamp = (now or datetime.now().astimezone()).strftime("%H%M%S_%f")
    return f"{timestamp}_{name}"


def run_directory(kind: str, run_id: str, *, now: datetime | None = None) -> Path:
    """Return ``results/runs/YYYY-MM-DD/<kind>/<run_id>`` without creating it."""
    if kind not in RUN_KINDS:
        raise ValueError(f"Unsupported run kind: {kind}")
    name = Path(run_id)
    if not run_id or name.name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a single directory name")
    date = (now or datetime.now().astimezone()).strftime("%Y-%m-%d")
    return ROOT / "results" / "runs" / date / kind / run_id


def create_run_directory(kind: str, run_id: str, *, now: datetime | None = None) -> Path:
    """Create and return the standard directory for one new run."""
    directory = run_directory(kind, run_id, now=now)
    directory.mkdir(parents=True, exist_ok=False)
    return directory
