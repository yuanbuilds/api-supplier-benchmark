#!/usr/bin/env python3
"""Unified workbench runner for chat, multimodal, and image-generation batches."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.catalog import ROOT, batch_engine, load_dotenv, tool_status, validate_batch
from workbench.config import load_yaml
from workbench.runs import read_engine_handoff


STATUS_LABELS = {"success": "成功", "passed": "通过", "partial": "部分通过", "failed": "失败", "error": "错误", "unknown": "未知"}
TREND_LABELS = {"stable": "稳定", "regression": "退化", "improvement": "改善", "needs_more_data": "证据不足"}
ENGINE_LABELS = {"evalscope_perf": "性能评测", "image_generation": "生图评测"}
QUALITY_REVIEW_TEST_TYPES = {"quality_regression", "resolution_detail_regression"}


def requires_human_quality_review(summary: dict) -> bool:
    plan = summary.get("plan") or {}
    return plan.get("evaluation_purpose") == "visual" or str(plan.get("test_type") or "") in QUALITY_REVIEW_TEST_TYPES


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one validated benchmark batch.")
    parser.add_argument("batch_file", help="For example evals/performance/evalscope/chat.yaml or evals/image/base/text-to-image.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and preview without calling a model.")
    parser.add_argument("--show-request", action="store_true", help="With --dry-run, print the sanitized image request body and credential fingerprint.")
    parser.add_argument("--confirm-cost", action="store_true", help="Explicitly allow an image batch above the configured estimated-cost threshold.")
    parser.add_argument("--html", action="store_true", help="Generate the optional self-contained HTML report after the run completes.")
    progress_group = parser.add_mutually_exclusive_group()
    progress_group.add_argument("--dynamic-progress", action="store_true", help="Use an ANSI one-line live progress bar (only for compatible terminals).")
    progress_group.add_argument("--plain-progress", action="store_true", help="Use newline-only submission progress (the default).")
    parser.add_argument("--result-file", help="Write a machine-readable run-result handoff for a caller.")
    args = parser.parse_args()
    if args.show_request and not args.dry_run:
        parser.error("--show-request requires --dry-run")
    return args


def image_progress_flag(dynamic_progress: bool) -> str:
    """Default to plain output because the unified runner may be embedded in a non-ANSI panel."""
    return "--dynamic-progress" if dynamic_progress else "--plain-progress"


def write_result_handoff(path: str | None, run_dir: Path | None, status: str) -> None:
    if path:
        Path(path).write_text(json.dumps({"run_dir": str(run_dir) if run_dir else None, "summary_path": str(run_dir / "workbench-summary.json") if run_dir else None, "status": status}, ensure_ascii=False) + "\n", encoding="utf-8")


def standard_entries(run_dir: Path, batch: dict, engine: str) -> list[dict]:
    modality = batch.get("test", {}).get("modality", "image_generation" if engine == "image_generation" else "chat")
    if engine == "image_generation":
        payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return [
            {"provider": item["target"]["provider"], "model": item["target"]["model"], "modality": modality, "status": "partial" if requires_human_quality_review(item) else "success" if item["strict_success_rate"] == 1 else "partial", "success_rate": item["strict_success_rate"], "latency_p95_ms": item["latency_seconds"]["p95"] * 1000 if item["latency_seconds"]["p95"] is not None else None, "metrics": item}
            for item in payload["targets"]
        ]
    with (run_dir / "summary.csv").open(encoding="utf-8", newline="") as source:
        return [
            {"provider": row["provider"], "model": row["model"], "modality": row.get("modality", modality), "status": row["status"], "success_rate": float(row["success_rate"]) if row.get("success_rate") else None, "latency_p95_ms": float(row["e2e_p95_ms"]) if row.get("e2e_p95_ms") else None, "metrics": row}
            for row in csv.DictReader(source)
        ]


def failed_entries(run_dir: Path, batch: dict, engine: str) -> list[dict]:
    """Expose failure metadata to automation without creating a benchmark conclusion."""
    modality = batch.get("test", {}).get("modality", "image_generation" if engine == "image_generation" else "chat")
    if engine == "image_generation" and (run_dir / "summary.json").exists():
        entries = standard_entries(run_dir, batch, engine)
        for entry in entries:
            if entry["status"] != "success":
                entry["status"] = "failed"
        return entries

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = []
    for command in manifest.get("commands", []):
        reason = command.get("reason") or (f"EvalScope exited with code {command['exit_code']}" if command.get("exit_code") else "评测任务未完成")
        log_path = ROOT / command.get("output_dir", "") / "benchmark.log"
        if log_path.exists():
            from workbench.engines.evalscope import failure_reason
            reason = failure_reason(log_path)
        entries.append({
            "provider": command.get("provider"),
            "model": command.get("model"),
            "modality": command.get("modality", modality),
            "status": "failed" if command.get("status") in {"failed", "skipped"} else "unknown",
            "success_rate": None,
            "latency_p95_ms": None,
            "error": reason,
            "metrics": {"exit_code": command.get("exit_code")},
        })
    return entries


def write_workbench_summary(run_dir: Path, batch_path: Path, engine: str, results: list[dict], status: str) -> Path:
    summary = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "batch_file": str(batch_path.relative_to(ROOT)),
        "engine": engine,
        "status": status,
        "conclusion": None,
        "created_at": datetime.now().astimezone().isoformat(),
        "results": results,
    }
    summary_path = run_dir / "workbench-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary_path


def write_trend_report(run_dir: Path, summary_path: Path) -> dict:
    from workbench.reports.trends import write_trend

    thresholds = load_yaml(ROOT / "configs" / "admission.yaml").get("default", {}).get("trend", {})
    return write_trend(run_dir, summary_path, thresholds)


def display_width(value: str) -> int:
    """Measure terminal columns, counting East Asian wide characters as two cells."""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def pad_terminal(value: str, width: int, align: str = "left") -> str:
    padding = max(width - display_width(value), 0)
    return f"{' ' * padding}{value}" if align == "right" else f"{value}{' ' * padding}"


def admission_summary(run_dir: Path) -> tuple[str, str]:
    path = run_dir / "admission.json"
    if not path.exists():
        return "未生成", "needs_more_data"
    payload = json.loads(path.read_text(encoding="utf-8"))
    decisions = [str(item.get("decision", "needs_more_data")) for item in payload.get("results", [])]
    if not decisions:
        return "未生成", "needs_more_data"
    approved = sum(decision == "approved" for decision in decisions)
    if approved == len(decisions):
        return f"已通过（{approved}/{len(decisions)}）", "approved"
    if any(decision == "rejected" for decision in decisions):
        return f"未通过（{approved}/{len(decisions)}）", "rejected"
    return f"证据不足（{approved}/{len(decisions)}）", "needs_more_data"


def print_completion_summary(run_dir: Path, results: list[dict], summary_path: Path, trend: dict, include_html: bool) -> None:
    total = len(results)
    successful = sum(str(item.get("status", "")).lower() in {"success", "passed"} for item in results)
    trend_conclusion = str(trend.get("conclusion", "unknown"))
    admission_text, _ = admission_summary(run_dir)
    execution_text = "成功" if successful == total and total else "失败或不完整"
    rows = []
    for item in results:
        success = item.get("success_rate")
        p95 = item.get("latency_p95_ms")
        metrics = item.get("metrics") or {}
        ttft = metrics.get("ttft_p95_ms")
        success_text = f"{float(success):.1%}" if isinstance(success, (int, float)) else "–"
        p95_text = f"{float(p95):.1f} ms" if isinstance(p95, (int, float)) else "–"
        try:
            ttft_text = f"{float(ttft):.1f} ms"
        except (TypeError, ValueError):
            ttft_text = "–"
        rows.append((f"{item.get('provider')} / {item.get('model')}", success_text, p95_text, ttft_text))
    provider_width = max(22, display_width("供应商 / 模型"), *(display_width(row[0]) for row in rows))
    success_width = max(9, display_width("成功率"), *(display_width(row[1]) for row in rows))
    p95_width = max(15, display_width("端到端 P95"), *(display_width(row[2]) for row in rows))
    ttft_width = max(15, display_width("首 Token P95"), *(display_width(row[3]) for row in rows))
    table_width = provider_width + success_width + p95_width + ttft_width + 8
    print("\n" + "═" * 72)
    print(f"  执行：{execution_text}（{successful}/{total}）  ·  准入：{admission_text}  ·  趋势：{TREND_LABELS.get(trend_conclusion, trend_conclusion)}")
    print("─" * table_width)
    print("  " + "  ".join((
        pad_terminal("供应商 / 模型", provider_width),
        pad_terminal("成功率", success_width, "right"),
        pad_terminal("端到端 P95", p95_width, "right"),
        pad_terminal("首 Token P95", ttft_width, "right"),
    )))
    for row in rows:
        print("  " + "  ".join((
            pad_terminal(row[0], provider_width),
            pad_terminal(row[1], success_width, "right"),
            pad_terminal(row[2], p95_width, "right"),
            pad_terminal(row[3], ttft_width, "right"),
        )))
    print("─" * table_width)
    if include_html:
        print("  可视化报告：")
        print(f"    {(run_dir / 'report.html').resolve()}")
    print("  统一摘要：")
    print(f"    {summary_path.resolve()}")
    print("═" * 72, flush=True)


def print_image_completion_summary(run_dir: Path, results: list[dict], summary_path: Path, include_html: bool) -> None:
    total = len(results)
    strict_successes = sum(item.get("success_rate") == 1 for item in results)
    request_total = sum(int((item.get("metrics") or {}).get("requests") or 0) for item in results)
    request_successes = sum(
        sum(bool(record.get("strict_success")) for record in (item.get("metrics") or {}).get("records") or [])
        for item in results
    )
    quality_review = bool(results) and all(requires_human_quality_review(item.get("metrics") or {}) for item in results)
    rows = []
    error_summaries = []
    error_details = []
    for item in results:
        metrics = item.get("metrics") or {}
        label = f"{item.get('provider')} / {item.get('model')}"
        rows.append((
            label,
            f"{float(metrics.get('strict_success_rate') or 0):.1%}",
            f"{float(metrics.get('valid_image_rate') or 0):.1%}",
            f"{float(metrics.get('dimension_match_rate') or 0):.1%}",
            f"{float(metrics.get('format_match_rate') or 0):.1%}",
            f"{float((metrics.get('latency_seconds') or {}).get('p95') or 0):.2f} s",
        ))
        errors = metrics.get("error_counts") or {}
        if errors:
            error_summaries.append(f"{label}：" + "，".join(f"{name} ×{count}" for name, count in errors.items()))
        failed_records = [record for record in metrics.get("records") or [] if not record.get("strict_success")]
        if failed_records:
            from workbench.engines.image_generation import terminal_result_detail

            for record in failed_records[:3]:
                error_details.append(
                    f"{label} · {record.get('size', '未知尺寸')} · {terminal_result_detail(record)}"
                )
            if len(failed_records) > 3:
                error_details.append(f"{label}：另有 {len(failed_records) - 3} 条失败请求，详见 records.jsonl")
    widths = [
        max(22, display_width("供应商 / 模型"), *(display_width(row[0]) for row in rows)),
        max(10, display_width("自动参数成功" if quality_review else "严格成功"), *(display_width(row[1]) for row in rows)),
        max(10, display_width("有效图片"), *(display_width(row[2]) for row in rows)),
        max(10, display_width("尺寸匹配"), *(display_width(row[3]) for row in rows)),
        max(10, display_width("格式匹配"), *(display_width(row[4]) for row in rows)),
        max(11, display_width("P95 耗时"), *(display_width(row[5]) for row in rows)),
    ]
    table_width = sum(widths) + 10
    execution_text = "自动参数校验完成 · 人工质量待复核" if quality_review else "通过" if strict_successes == total and total else "存在不通过"
    print("\n" + "═" * 72)
    if quality_review:
        print(f"  生图质量：{execution_text}  ·  目标自动通过 {strict_successes}/{total}  ·  请求自动通过 {request_successes}/{request_total}")
    else:
        print(f"  生图结果：{execution_text}  ·  目标严格通过 {strict_successes}/{total}  ·  请求严格通过 {request_successes}/{request_total}")
    print("─" * table_width)
    headers = ("供应商 / 模型", "自动参数成功" if quality_review else "严格成功", "有效图片", "尺寸匹配", "格式匹配", "P95 耗时")
    print("  " + "  ".join(pad_terminal(header, width, "left" if index == 0 else "right") for index, (header, width) in enumerate(zip(headers, widths))))
    for row in rows:
        print("  " + "  ".join(pad_terminal(value, widths[index], "left" if index == 0 else "right") for index, value in enumerate(row)))
    print("─" * table_width)
    for error_summary in error_summaries:
        print(f"  错误摘要：{error_summary}")
    for error_detail in error_details:
        print(f"  错误详情：{error_detail}")
    for item in results:
        metrics = item.get("metrics") or {}
        rate = metrics.get("rate_control") or {}
        if rate:
            print(
                f"  RPM：{item.get('provider')} / {item.get('model')} · 计划 {rate.get('planned_rpm')} · "
                f"实际发起 {rate.get('actual_start_rpm')} · 完成 {rate.get('completion_rpm')} · "
                f"峰值在途 {rate.get('peak_in_flight')}",
            )
    if include_html:
        print("  可视化报告：")
        print(f"    {(run_dir / 'report.html').resolve()}")
    print("  统一摘要：")
    print(f"    {summary_path.resolve()}")
    print("═" * 72, flush=True)


def main() -> int:
    args = parse_args()
    batch_path = (ROOT / args.batch_file).resolve()
    try:
        preview = load_yaml(batch_path)
    except (OSError, ValueError) as error:
        print(f"Preflight failed: {error}", file=sys.stderr)
        write_result_handoff(args.result_file, None, "failed")
        return 2
    preview_test = preview.get("test")
    if not isinstance(preview_test, dict) or preview_test.get("supplier_acceptance") is not True:
        load_dotenv()
    batch, errors, warnings = validate_batch(batch_path, allow_cost_overrun=args.confirm_cost)
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        print("Preflight failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        write_result_handoff(args.result_file, None, "failed")
        return 2
    engine = batch_engine(batch)
    tool = tool_status(engine)
    if not args.dry_run and not tool["tool_ready"]:
        print(f"Preflight failed: 缺少运行工具 {tool['tool']}。请先执行 python3 scripts/check_env.py {args.batch_file} 查看处理建议。", file=sys.stderr)
        write_result_handoff(args.result_file, None, "failed")
        return 2
    module = "workbench.orchestrators.evalscope" if engine == "evalscope_perf" else "workbench.engines.image_generation"
    # Keep child output unbuffered so long-running image generation shows progress.
    command = [sys.executable, "-u", "-m", module, args.batch_file]
    if engine == "image_generation":
        command.append(image_progress_flag(args.dynamic_progress))
    if args.dry_run:
        command.append("--dry-run")
    if args.show_request:
        command.append("--show-request")
    if args.html and engine == "image_generation":
        command.append("--html")
    print(f"开始运行：{batch.get('name', batch_path.stem)}（{ENGINE_LABELS.get(engine, engine)}）", flush=True)
    with tempfile.TemporaryDirectory(prefix="workbench-engine-result-") as temp_dir:
        engine_result_path = Path(temp_dir) / "result.json"
        command.extend(["--result-file", str(engine_result_path)])
        process = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        assert process.stdout is not None
        while chunk := process.stdout.read(1):
            print(chunk, end="", flush=True)
        return_code = process.wait()
        if args.dry_run:
            return return_code
        try:
            run_dir = read_engine_handoff(engine_result_path)
        except ValueError as error:
            print(f"ERROR: child runner returned an invalid run-result handoff: {error}", file=sys.stderr)
            write_result_handoff(args.result_file, None, "failed")
            return return_code or 1
    if return_code != 0:
        results = failed_entries(run_dir, batch, engine)
        summary_path = write_workbench_summary(run_dir, batch_path, engine, results, "failed")
        write_result_handoff(args.result_file, run_dir, "failed")
        if engine == "image_generation":
            print_image_completion_summary(run_dir, results, summary_path, args.html)
        else:
            print(f"失败摘要: {summary_path}")
        return return_code
    if engine == "evalscope_perf":
        from workbench.orchestrators.post_run import finalize_evalscope

        try:
            if batch.get("test", {}).get("supplier_acceptance"):
                # The supplier plan owns its thresholds. Do not apply unrelated
                # internal admission defaults to an external measurement run.
                from workbench.reports.evalscope import refresh_reports
                refresh_reports(run_dir, include_html=args.html)
            else:
                finalize_evalscope(run_dir, include_html=args.html)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
            print(f"ERROR: 评测已执行，但结果汇总失败：{error}", file=sys.stderr)
            write_result_handoff(args.result_file, run_dir, "failed")
            return 1
    results = standard_entries(run_dir, batch, engine)
    summary_path = write_workbench_summary(run_dir, batch_path, engine, results, "completed")
    write_result_handoff(args.result_file, run_dir, "completed")
    if engine == "image_generation":
        print_image_completion_summary(run_dir, results, summary_path, args.html)
        return 0
    trend = {"conclusion": "needs_more_data", "reason": "供应商专项使用该次计划的阈值，不继承内部准入标准。"} if batch.get("test", {}).get("supplier_acceptance") else write_trend_report(run_dir, summary_path)
    print_completion_summary(run_dir, results, summary_path, trend, args.html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
