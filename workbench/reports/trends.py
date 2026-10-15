"""Compare a completed workbench summary with the latest equivalent run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def numeric(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def key(result: dict) -> tuple[str | None, str | None, str | None]:
    return result.get("provider"), result.get("model"), result.get("modality")


def compare_summaries(current: dict, baseline: dict, thresholds: dict) -> dict:
    baseline_results = {key(item): item for item in baseline.get("results", [])}
    comparisons, regressions = [], []
    for item in current.get("results", []):
        previous = baseline_results.get(key(item))
        comparison = {"provider": item.get("provider"), "model": item.get("model"), "modality": item.get("modality"), "baseline_found": previous is not None, "changes": []}
        if previous is None:
            comparisons.append(comparison)
            continue
        for field, limit_key, higher_is_worse in (
            ("latency_p95_ms", "p95_latency_regression_max_percent", True),
            ("success_rate", "success_rate_drop_max", False),
        ):
            old, new = numeric(previous.get(field)), numeric(item.get(field))
            if old is None or new is None:
                continue
            if field == "latency_p95_ms" and old > 0:
                change = (new - old) / old
            else:
                change = new - old
            threshold = numeric(thresholds.get(limit_key))
            regressed = threshold is not None and ((higher_is_worse and change > threshold) or (not higher_is_worse and change < -threshold))
            detail = {"metric": field, "baseline": old, "current": new, "change": round(change, 6), "regressed": regressed}
            comparison["changes"].append(detail)
            if regressed:
                regressions.append({**comparison, "change": detail})
        comparisons.append(comparison)
    return {"baseline_run_id": baseline.get("run_id"), "current_run_id": current.get("run_id"), "conclusion": "regression" if regressions else "stable", "comparisons": comparisons, "regressions": regressions}


def latest_baseline(run_dir: Path, summary: dict) -> Path | None:
    candidates = []
    for path in (ROOT / "results" / "runs").rglob("workbench-summary.json"):
        if path.parent == run_dir:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("status") == "completed" and payload.get("batch_file") == summary.get("batch_file"):
            candidates.append(path)
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def write_trend(run_dir: Path, summary_path: Path, thresholds: dict) -> dict:
    current = json.loads(summary_path.read_text(encoding="utf-8"))
    baseline_path = latest_baseline(run_dir, current)
    if baseline_path is None:
        payload = {"current_run_id": current.get("run_id"), "baseline_run_id": None, "conclusion": "no_baseline", "comparisons": [], "regressions": []}
    else:
        payload = compare_summaries(current, json.loads(baseline_path.read_text(encoding="utf-8")), thresholds)
        payload["baseline_summary"] = str(baseline_path.relative_to(ROOT))
    (run_dir / "trend.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
