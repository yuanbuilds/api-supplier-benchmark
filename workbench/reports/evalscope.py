#!/usr/bin/env python3
"""Summarize an EvalScope benchmark run directory.

The script reads `manifest.json`, parses EvalScope JSON artifacts when present,
and writes:

- `summary.csv`
- optional `report-data.json` and `report.html`
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from workbench.reports.ui import performance_report_data, write_static_report


ROOT = Path(__file__).resolve().parents[2]

SUMMARY_FIELDS = [
    "run_id",
    "batch_name",
    "provider",
    "model",
    "modality",
    "dataset",
    "image_num",
    "image_size",
    "parallel",
    "number",
    "status",
    "exit_code",
    "success_rate",
    "error_rate",
    "timeout_rate",
    "empty_output_rate",
    "ttft_avg_ms",
    "ttft_p50_ms",
    "ttft_p95_ms",
    "ttft_p99_ms",
    "e2e_avg_ms",
    "e2e_p50_ms",
    "e2e_p95_ms",
    "e2e_p99_ms",
    "tokens_per_second",
    "request_throughput_rps",
    "planned_requests_per_minute",
    "actual_requests_per_minute",
    "total_requests",
    "success_requests",
    "failed_requests",
    "cost_estimate",
    "output_dir",
    "error",
]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def find_evalscope_result_dirs(output_dir: Path) -> list[Path]:
    return sorted(path.parent for path in output_dir.rglob("benchmark_summary.json"))


def percentile_row(rows: list[dict], label: str) -> dict:
    for row in rows:
        if str(row.get("Percentiles")) == label:
            return row
    return {}


def ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if denominator in (None, 0):
        return None
    return round(float(numerator or 0) / float(denominator), 6)


def as_ms_from_seconds(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) * 1000, 3)


def read_error(output_dir: Path) -> str:
    log_files = sorted(output_dir.rglob("benchmark.log"))
    for log_file in reversed(log_files):
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        error_lines = [line for line in lines if " ERROR:" in line or "Traceback" in line]
        if error_lines:
            return sanitize_error(error_lines[-1])
    return ""


def sanitize_error(value: str) -> str:
    value = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
    value = re.sub(r"sk-[A-Za-z0-9]+", "sk-***", value)
    return value


def build_empty_row(manifest: dict, command: dict, output_dir: Path) -> dict:
    modality = command.get("modality", "text")
    return {
        "run_id": manifest.get("run_id"),
        "batch_name": manifest.get("batch_name"),
        "provider": command.get("provider"),
        "model": command.get("model"),
        "modality": modality,
        "dataset": command.get("dataset"),
        "image_num": image_count(command, {}, modality),
        "image_size": image_dimensions(command, {}, modality),
        "parallel": join_list(command.get("parallel")),
        "number": join_list(command.get("number")),
        "status": command.get("status", "unknown"),
        "exit_code": command.get("exit_code", ""),
        "success_rate": "",
        "error_rate": "",
        "timeout_rate": "",
        "empty_output_rate": "",
        "ttft_avg_ms": "",
        "ttft_p50_ms": "",
        "ttft_p95_ms": "",
        "ttft_p99_ms": "",
        "e2e_avg_ms": "",
        "e2e_p50_ms": "",
        "e2e_p95_ms": "",
        "e2e_p99_ms": "",
        "tokens_per_second": "",
        "request_throughput_rps": "",
        "planned_requests_per_minute": command.get("planned_requests_per_minute", ""),
        "actual_requests_per_minute": "",
        "total_requests": "",
        "success_requests": "",
        "failed_requests": "",
        "cost_estimate": "",
        "output_dir": rel(output_dir),
        "error": command.get("reason", "") or read_error(output_dir),
    }


def join_list(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return "" if value is None else str(value)


def build_result_row(manifest: dict, command: dict, result_dir: Path) -> dict:
    summary = load_json(result_dir / "benchmark_summary.json")
    percentile_path = result_dir / "benchmark_percentile.json"
    percentile = load_json(percentile_path) if percentile_path.is_file() else []
    args_path = result_dir / "benchmark_args.json"
    args = load_json(args_path) if args_path.exists() else {}

    p50 = percentile_row(percentile, "50%")
    p95 = percentile_row(percentile, "95%")
    p99 = percentile_row(percentile, "99%")

    total = summary.get("Total Requests")
    success = summary.get("Success Requests")
    failed = summary.get("Failed Requests")
    modality = command.get("modality", "text")

    return {
        "run_id": manifest.get("run_id"),
        "batch_name": manifest.get("batch_name"),
        "provider": command.get("provider"),
        "model": command.get("model"),
        "modality": modality,
        "dataset": command.get("dataset") or args.get("dataset"),
        "image_num": image_count(command, args, modality),
        "image_size": image_dimensions(command, args, modality),
        "parallel": args.get("parallel") or summary.get("Concurrency") or join_list(command.get("parallel")),
        "number": args.get("number") or total or join_list(command.get("number")),
        "status": command.get("status", "unknown"),
        "exit_code": command.get("exit_code", ""),
        "success_rate": ratio(success, total),
        "error_rate": ratio(failed, total),
        "timeout_rate": "",
        "empty_output_rate": "",
        "ttft_avg_ms": summary.get("Avg TTFT (ms)", summary.get("TTFT (ms)", "")),
        "ttft_p50_ms": p50.get("TTFT (ms)", ""),
        "ttft_p95_ms": p95.get("TTFT (ms)", ""),
        "ttft_p99_ms": p99.get("TTFT (ms)", ""),
        "e2e_avg_ms": as_ms_from_seconds(summary.get("Avg Latency (s)")),
        "e2e_p50_ms": as_ms_from_seconds(p50.get("Latency (s)")),
        "e2e_p95_ms": as_ms_from_seconds(p95.get("Latency (s)")),
        "e2e_p99_ms": as_ms_from_seconds(p99.get("Latency (s)")),
        "tokens_per_second": summary.get("Output Throughput (tok/s)", ""),
        "request_throughput_rps": summary.get("Req Throughput (req/s)", ""),
        "planned_requests_per_minute": command.get("planned_requests_per_minute", ""),
        "actual_requests_per_minute": "" if command.get("supplier_acceptance") else round(as_float(summary.get("Req Throughput (req/s)")) * 60, 3) if as_float(summary.get("Req Throughput (req/s)")) is not None else "",
        "total_requests": total,
        "success_requests": success,
        "failed_requests": failed,
        "cost_estimate": "",
        "output_dir": rel(result_dir),
        "error": read_error(result_dir),
    }


def build_rows(run_dir: Path, manifest: dict) -> list[dict]:
    rows = []
    for command in manifest.get("commands", []):
        output_dir = ROOT / command["output_dir"]
        result_dirs = find_evalscope_result_dirs(output_dir)
        if not result_dirs:
            rows.append(build_empty_row(manifest, command, output_dir))
            continue
        for result_dir in result_dirs:
            rows.append(build_result_row(manifest, command, result_dir))
    return rows


def write_summary_csv(run_dir: Path, rows: list[dict]) -> Path:
    summary_path = run_dir / "summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SUMMARY_FIELDS})
    return summary_path


def fmt(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def has_image_input(modality: str | None) -> bool:
    return modality in {"multimodal", "vision", "image"}


def image_count(command: dict, args: dict, modality: str | None) -> str:
    if not has_image_input(modality):
        return ""
    return command.get("image_num") or args.get("image_num", "")


def image_dimensions(command: dict, args: dict, modality: str | None) -> str:
    if not has_image_input(modality):
        return ""
    return image_size(command) or image_size(args)


def image_size(mapping: dict) -> str:
    width = mapping.get("image_width")
    height = mapping.get("image_height")
    if width in ("", None) or height in ("", None):
        return ""
    return f"{width}x{height}"


def as_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percent(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return "-"
    return f"{number * 100:.1f}%"


def metric(value: Any, suffix: str = "", precision: int = 2) -> str:
    number = as_float(value)
    if number is None:
        return "-"
    return f"{number:.{precision}f}{suffix}"


def status_icon(status: str) -> str:
    return {
        "success": "PASS",
        "failed": "FAIL",
        "skipped": "SKIP",
        "dry_run": "DRY",
    }.get(status, status.upper() if status else "UNKNOWN")


def aggregate(rows: list[dict]) -> dict:
    total_requests = sum(as_float(row.get("total_requests")) or 0 for row in rows)
    success_requests = sum(as_float(row.get("success_requests")) or 0 for row in rows)
    failed_requests = sum(as_float(row.get("failed_requests")) or 0 for row in rows)
    success_rates = [as_float(row.get("success_rate")) for row in rows if as_float(row.get("success_rate")) is not None]
    p95_values = [as_float(row.get("e2e_p95_ms")) for row in rows if as_float(row.get("e2e_p95_ms")) is not None]
    tps_values = [as_float(row.get("tokens_per_second")) for row in rows if as_float(row.get("tokens_per_second")) is not None]

    return {
        "total_requests": int(total_requests),
        "success_requests": int(success_requests),
        "failed_requests": int(failed_requests),
        "avg_success_rate": round(sum(success_rates) / len(success_rates), 6) if success_rates else None,
        "max_p95_latency_ms": max(p95_values) if p95_values else None,
        "avg_tokens_per_second": round(sum(tps_values) / len(tps_values), 3) if tps_values else None,
    }


def row_health(row: dict) -> str:
    if row.get("status") in {"dry_run", "skipped"}:
        return "not measured"
    if row.get("status") != "success":
        return "needs attention"
    success_rate = as_float(row.get("success_rate"))
    p95 = as_float(row.get("e2e_p95_ms"))
    if success_rate is not None and success_rate < 0.99:
        return "needs attention"
    if p95 is not None and p95 > 10000:
        return "watch latency"
    return "healthy"


def load_admission(run_dir: Path) -> dict | None:
    path = run_dir / "admission.json"
    if not path.exists():
        return None
    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def admission_html(payload: dict | None) -> str:
    if not payload:
        return "<p class='empty'>Not generated yet. Run the batch through <code>scripts/run_benchmark.py</code> to generate admission.</p>"
    rows = []
    for item in payload.get("results", []):
        decision = fmt(item.get("decision"))
        reasons = "; ".join(item.get("reasons", []))
        rows.append(
            "<tr>"
            f"<td>{html.escape(fmt(item.get('provider')))}</td>"
            f"<td><strong>{html.escape(fmt(item.get('model')))}</strong></td>"
            f"<td><span class='decision decision-{html.escape(decision)}'>{html.escape(decision)}</span></td>"
            f"<td>{html.escape(reasons)}</td>"
            "</tr>"
        )
    return "<table><thead><tr><th>Provider</th><th>Model</th><th>Decision</th><th>Reasons</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def write_legacy_html_report(run_dir: Path, manifest: dict, rows: list[dict], admission: dict | None) -> Path:
    report_path = run_dir / "report-legacy.html"
    statuses = Counter(row.get("status", "unknown") for row in rows)
    totals = aggregate(rows)
    include_images = any(has_image_input(row.get("modality")) for row in rows)

    cards = [
        ("Models", str(len(rows)), "model rows"),
        ("Requests", str(totals["total_requests"]), f"{totals['success_requests']} success"),
        ("Success Rate", percent(totals["avg_success_rate"]), "average"),
        ("P95 Latency", metric(totals["max_p95_latency_ms"], " ms", 1), "max"),
        ("Throughput", metric(totals["avg_tokens_per_second"], " tok/s", 2), "average output"),
    ]

    rows_html = []
    for row in rows:
        health = row_health(row)
        status = fmt(row.get("status"))
        image_cell = f"<td>{html.escape(fmt(row.get('image_num')) or '-')}</td>" if include_images else ""
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(fmt(row.get('provider')))}</td>"
            f"<td><strong>{html.escape(fmt(row.get('model')))}</strong></td>"
            f"<td>{html.escape(fmt(row.get('modality')))}</td>"
            f"<td><span class='badge badge-{html.escape(status)}'>{html.escape(status_icon(status))}</span></td>"
            f"<td>{html.escape(percent(row.get('success_rate')))}</td>"
            f"<td>{html.escape(percent(row.get('error_rate')))}</td>"
            f"<td>{html.escape(metric(row.get('e2e_p50_ms'), ' ms', 1))}</td>"
            f"<td>{html.escape(metric(row.get('e2e_p95_ms'), ' ms', 1))}</td>"
            f"<td>{html.escape(metric(row.get('ttft_p95_ms'), ' ms', 1))}</td>"
            f"<td>{html.escape(metric(row.get('tokens_per_second'), ' tok/s', 2))}</td>"
            f"{image_cell}"
            f"<td><span class='health'>{html.escape(health)}</span></td>"
            f"<td class='path'>{html.escape(fmt(row.get('output_dir')))}</td>"
            "</tr>"
        )

    status_items = "".join(
        f"<span class='status-pill'>{html.escape(status_icon(status))}: {count}</span>"
        for status, count in sorted(statuses.items())
    )
    cards_html = "".join(
        f"<section class='metric-card'><div class='metric-label'>{html.escape(label)}</div><div class='metric-value'>{html.escape(value)}</div><div class='metric-note'>{html.escape(note)}</div></section>"
        for label, value, note in cards
    )
    image_header = "<th>Images</th>" if include_images else ""
    admission_section = admission_html(admission)

    html_text = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Benchmark Report - {html.escape(fmt(manifest.get('run_id')))}</title>
  <style>
    :root {{
      --bg: #0c0c1a;
      --panel: #12122b;
      --panel-2: #171735;
      --border: rgba(129, 109, 248, 0.2);
      --text: #e2e8f0;
      --muted: #93a4b8;
      --accent: #816df8;
      --green: #12b886;
      --yellow: #f59f00;
      --red: #ff6b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      line-height: 1.55;
    }}
    .container {{ max-width: 1180px; margin: 0 auto; padding: 32px 24px 64px; }}
    .hero {{
      border: 1px solid var(--border);
      background: linear-gradient(135deg, #11112c 0%, #171942 56%, #0d1024 100%);
      border-radius: 18px;
      padding: 28px 30px;
      margin-bottom: 24px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .subtitle {{ color: var(--muted); }}
    .status-row {{ margin-top: 18px; display: flex; flex-wrap: wrap; gap: 10px; }}
    .status-pill {{
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(129, 109, 248, 0.1);
      color: var(--text);
      padding: 5px 10px;
      font-size: 13px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .metric-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
    }}
    .metric-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
    .metric-value {{ font-size: 27px; font-weight: 720; margin-top: 7px; }}
    .metric-note {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
    }}
    .panel-header {{ padding: 18px 20px; border-bottom: 1px solid var(--border); background: var(--panel-2); }}
    .panel-header h2 {{ margin: 0; font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid rgba(129,109,248,.11); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; background: rgba(255,255,255,.02); }}
    tr:hover td {{ background: rgba(129,109,248,.05); }}
    .badge {{ border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 700; }}
    .badge-success {{ background: rgba(18,184,134,.15); color: var(--green); }}
    .badge-failed {{ background: rgba(255,107,107,.15); color: var(--red); }}
    .badge-skipped, .badge-dry_run {{ background: rgba(245,159,0,.15); color: var(--yellow); }}
    .decision {{ border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 700; }}
    .decision-approved {{ background: rgba(18,184,134,.15); color: var(--green); }}
    .decision-rejected {{ background: rgba(255,107,107,.15); color: var(--red); }}
    .decision-needs_more_data {{ background: rgba(245,159,0,.15); color: var(--yellow); }}
    .empty {{ padding: 20px; color: var(--muted); }}
    .health {{ color: var(--muted); }}
    .path {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; max-width: 340px; word-break: break-all; }}
    .admission-panel {{ margin-bottom: 24px; }}
    .footer {{ color: var(--muted); margin-top: 18px; font-size: 13px; }}
  </style>
</head>
<body>
  <main class="container">
    <section class="hero">
      <h1>Benchmark Report</h1>
      <div class="subtitle">Run <strong>{html.escape(fmt(manifest.get('run_id')))}</strong> · Batch <strong>{html.escape(fmt(manifest.get('batch_name')))}</strong> · Engine <strong>{html.escape(fmt(manifest.get('engine')))}</strong></div>
      <div class="status-row">{status_items}</div>
    </section>
    <section class="metric-grid">{cards_html}</section>
    <section class="panel admission-panel">
      <div class="panel-header"><h2>Admission Decision</h2></div>
      {admission_section}
    </section>
    <section class="panel">
      <div class="panel-header"><h2>Model Results</h2></div>
      <table>
        <thead>
          <tr>
            <th>Provider</th><th>Model</th><th>Modality</th><th>Status</th><th>Success</th><th>Error</th><th>P50 Latency</th><th>P95 Latency</th><th>TTFT P95</th><th>Throughput</th>{image_header}<th>Health</th><th>Output</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </section>
    <div class="footer">Generated from EvalScope artifacts by the model benchmark workbench.</div>
  </main>
</body>
</html>
"""
    report_path.write_text(html_text, encoding="utf-8")
    return report_path


def write_html_report(run_dir: Path, manifest: dict, rows: list[dict], admission: dict | None) -> Path:
    """Write the optional native report for this workbench-owned summary."""
    payload = performance_report_data(run_dir, manifest, rows, admission, aggregate(rows))
    (run_dir / "report-data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return write_static_report(run_dir / "report.html", payload)


def print_console_summary(manifest: dict, rows: list[dict], summary_path: Path, html_report_path: Path | None) -> None:
    providers = Counter(row["provider"] for row in rows)
    statuses = Counter(row.get("status", "unknown") for row in rows)

    print(f"Run: {manifest.get('run_id')}")
    print(f"Batch: {manifest.get('batch_name')}")
    print(f"Engine: {manifest.get('engine')}")
    print(f"Rows: {len(rows)}")
    print("Providers:")
    for provider, count in sorted(providers.items()):
        print(f"  - {provider}: {count}")
    print("Statuses:")
    for status, count in sorted(statuses.items()):
        print(f"  - {status}: {count}")
    print(f"Summary: {summary_path}")
    if html_report_path:
        print(f"HTML Report: {html_report_path}")


def refresh_reports(run_dir: Path, include_html: bool = True) -> tuple[Path, Path | None]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    manifest = load_json(manifest_path)
    rows = build_rows(run_dir, manifest)
    summary_path = write_summary_csv(run_dir, rows)
    admission = load_admission(run_dir)
    html_report_path = write_html_report(run_dir, manifest, rows, admission) if include_html else None
    return summary_path, html_report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize an EvalScope run directory.")
    parser.add_argument("run_dir")
    parser.add_argument("--html", action="store_true", help="Generate the optional self-contained HTML report.")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    try:
        summary_path, html_report_path = refresh_reports(run_dir, include_html=args.html)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    manifest = load_json(run_dir / "manifest.json")
    rows = build_rows(run_dir, manifest)
    print_console_summary(manifest, rows, summary_path, html_report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
