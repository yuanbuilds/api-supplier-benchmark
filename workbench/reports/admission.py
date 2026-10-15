#!/usr/bin/env python3
"""Generate a simple performance admission decision from summary.csv."""

from __future__ import annotations

import csv
import argparse
import json
import sys
from pathlib import Path

from workbench.config import load_yaml


ROOT = Path(__file__).resolve().parents[2]


def to_float(value: str) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def decide(row: dict, thresholds: dict) -> tuple[str, list[str]]:
    reasons = []
    performance = thresholds.get("performance", {})

    if row.get("status") in {"dry_run", "skipped"}:
        return "needs_more_data", [f"status is {row.get('status')}"]
    if row.get("status") != "success":
        return "rejected", [f"status is {row.get('status')}"]

    checks = [
        ("success_rate", ">=", performance.get("success_rate_min")),
        ("error_rate", "<=", performance.get("error_rate_max")),
        ("empty_output_rate", "<=", performance.get("empty_output_rate_max")),
        ("e2e_p95_ms", "<=", performance.get("p95_latency_max_ms")),
        ("tokens_per_second", ">=", performance.get("tokens_per_second_min")),
    ]

    missing = []
    failed = []
    for field, op, threshold in checks:
        if threshold is None:
            continue
        value = to_float(row.get(field, ""))
        if value is None:
            missing.append(field)
            continue
        if op == ">=" and value < float(threshold):
            failed.append(f"{field} {value} < {threshold}")
        if op == "<=" and value > float(threshold):
            failed.append(f"{field} {value} > {threshold}")

    if failed:
        return "rejected", failed
    if missing:
        return "needs_more_data", [f"missing {', '.join(missing)}"]
    return "approved", reasons


def write_admission(run_dir: Path, config_path: Path | None = None) -> dict:
    """Write the performance admission artifacts from an existing summary."""
    config_path = config_path or ROOT / "configs" / "admission.yaml"
    summary_path = run_dir / "summary.csv"
    manifest_path = run_dir / "manifest.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Summary not found: {summary_path}")

    thresholds = load_yaml(config_path).get("default", {})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    results = []
    with summary_path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            decision, reasons = decide(row, thresholds)
            results.append({"provider": row.get("provider"), "model": row.get("model"), "decision": decision, "reasons": reasons, "summary": row})
    payload = {"run_id": manifest.get("run_id", run_dir.name), "config": str(config_path.relative_to(ROOT)), "results": results}
    (run_dir / "admission.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an admission decision from a performance summary.")
    parser.add_argument("run_dir")
    parser.add_argument("config", nargs="?", default="configs/admission.yaml")
    parser.add_argument("--html", action="store_true", help="Generate the optional self-contained HTML report.")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    try:
        payload = write_admission(run_dir, config_path)
    except FileNotFoundError as error:
        print(f"{error}. Run the batch through scripts/run_benchmark.py first.", file=sys.stderr)
        return 1

    from workbench.reports.evalscope import refresh_reports

    refresh_reports(run_dir, include_html=args.html)

    print(f"Admission: {run_dir / 'admission.json'}")
    for item in payload["results"]:
        print(f"{item['provider']} / {item['model']}: {item['decision']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
