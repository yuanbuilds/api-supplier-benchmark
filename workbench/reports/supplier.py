"""Supplier-facing aggregation over complete EvalScope request evidence.

Only whitelisted scalar metrics are exported. No prompts, generated text,
response bodies, headers, or arbitrary exception messages enter this report.
The official engine remains the scheduler and retains its native evidence.
"""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any

from workbench.reports.ui import number, percent, write_static_report


NUMERIC_FIELDS = (
    "sequence", "started_epoch", "completed_epoch", "started_monotonic", "completed_monotonic",
    "start_offset_seconds", "scheduled_offset_seconds", "schedule_delay_ms", "ttft_seconds",
    "ttfot_seconds", "latency_seconds", "http_status", "prompt_tokens", "completion_tokens",
)
BOOL_FIELDS = ("strict_success", "done_received", "body_nonempty", "usage_present")
CSV_FIELDS = ("phase", *NUMERIC_FIELDS, *BOOL_FIELDS, "finish_reason", "error_code", "valid_success")
ERROR_CODES = {
    "TIMEOUT", "NETWORK_ERROR", "CLIENT_ERROR", "INVALID_UTF8", "MALFORMED_STREAM",
    "API_STREAM_ERROR", "UNSUPPORTED_CHOICES", "UNEXPECTED_TOOL_CALL", "NON_TEXT_CONTENT",
    "CONFLICTING_FINISH", "MISSING_DONE", "TRUNCATED", "MISSING_OR_INVALID_FINISH",
    "EMPTY_BODY", "NON_STREAM_RESPONSE", "DATA_AFTER_DONE", "CONTRACT_MISMATCH",
}
PHASE_LABELS = {"connectivity": "连通性", "latency": "低负载计时", "rpm50": "50 RPM",
                "rpm100": "100 RPM", "rpm200": "200 RPM"}
ASSESSMENT_LABELS = {"measurement_only": "已完成测量，未判定 SLA", "passed": "满足已设阈值",
                     "not_passed": "未达到本次测试要求", "incomplete": "测试或证据未完成，不能判断供应商"}


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def distribution(values: list[Any]) -> dict:
    clean = sorted(value for raw in values if (value := finite(raw)) is not None and value >= 0)
    def percentile(q):
        if not clean:
            return None
        rank = (len(clean) - 1) * q
        lower = int(rank)
        return clean[lower] + (clean[min(lower + 1, len(clean) - 1)] - clean[lower]) * (rank - lower)
    return {"count": len(clean), "p50": percentile(.5), "p95": percentile(.95),
            "p99": percentile(.99), "max": max(clean) if clean else None}


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and (value in ERROR_CODES or re.fullmatch(r"HTTP_[1-5][0-9]{2}", value)):
        return value
    return "UNKNOWN_ERROR"


def _normalize(raw: Any) -> tuple[dict, bool]:
    if not isinstance(raw, dict):
        return {}, False
    row = {key: finite(raw.get(key)) for key in NUMERIC_FIELDS}
    row.update({key: raw.get(key) is True for key in BOOL_FIELDS})
    finish = raw.get("finish_reason")
    row["finish_reason"] = finish if finish in (None, "stop", "length", "tool_calls", "content_filter") else "unknown"
    row["error_code"] = _safe_error(raw.get("error_code"))
    if row["sequence"] is not None and row["sequence"].is_integer():
        row["sequence"] = int(row["sequence"])
    required = ("started_epoch", "completed_epoch", "started_monotonic", "completed_monotonic", "latency_seconds")
    timing_ok = all(row[key] is not None for key in required)
    if timing_ok:
        timing_ok = (row["latency_seconds"] >= 0
                     and row["completed_monotonic"] >= row["started_monotonic"]
                     and row["completed_epoch"] >= row["started_epoch"]
                     and abs(row["completed_monotonic"] - row["started_monotonic"] - row["latency_seconds"]) < .01)
    sequence_ok = isinstance(row["sequence"], int) and row["sequence"] > 0
    schema_ok = raw.get("schema_version") == 1 and sequence_ok and timing_ok
    generated, content, latency = row["ttft_seconds"], row["ttfot_seconds"], row["latency_seconds"]
    token_timing_ok = (generated is not None and content is not None and latency is not None
                       and 0 <= generated <= content + 1e-6 and content <= latency + 1e-6)
    row["valid_success"] = (schema_ok and row["strict_success"] and row["done_received"]
                            and row["finish_reason"] == "stop" and row["body_nonempty"]
                            and row["http_status"] == 200 and row["error_code"] is None and token_timing_ok)
    if row["strict_success"] and not row["valid_success"]:
        row["error_code"] = "CONTRACT_MISMATCH"
        schema_ok = False
    if not row["valid_success"] and row["error_code"] is None:
        row["error_code"] = "UNKNOWN_ERROR"
    return row, schema_ok


def _load_one(directory: Path, name: str) -> tuple[Any, Path | None]:
    paths = sorted(path for path in directory.rglob(name) if path.is_file() and not path.is_symlink())
    if len(paths) != 1:
        return None, None
    try:
        return json.loads(paths[0].read_text(encoding="utf-8")), paths[0]
    except (OSError, ValueError):
        return None, paths[0]


def _rpm_summary(directory: Path, phase: dict, records: list[dict]) -> dict | None:
    rpm, window = finite(phase.get("rpm")), finite(phase.get("window_seconds"))
    if not rpm:
        return None
    result = {"planned_rpm": rpm, "window_seconds": window, "origin_kind": None,
              "actual_start_rpm": None, "completed_rpm_in_window": None,
              "successful_completed_rpm_in_window": None, "started_in_window": None,
              "completed_in_window": None, "successful_completed_in_window": None,
              "completed_after_window": None, "late_starts": None, "minutes": [],
              "schedule_delay_ms": distribution([]), "met": None,
              "tolerance": "固定窗口和每分钟允许 1 次边界请求；总记录数必须完全匹配，且发起跨度必须覆盖计划。"}
    schedule, _ = _load_one(directory, "supplier-schedule.json")
    if not isinstance(schedule, dict) or schedule.get("schema_version") != 1 or window is None or window <= 0:
        return result
    origin = finite(schedule.get("dispatch_ready_monotonic"))
    if origin is None or schedule.get("origin_kind") != "requests_ready_before_native_dispatch":
        return result
    result["origin_kind"] = schedule["origin_kind"]
    result["origin_note"] = "请求就绪时起算，含进入官方调度器开销；这是原生调度起点的保守下界。"
    offsets = [(row["started_monotonic"] - origin, row["completed_monotonic"] - origin, row) for row in records
               if row["started_monotonic"] is not None and row["completed_monotonic"] is not None]
    start_count = sum(0 <= start < window for start, _, _ in offsets)
    completed = sum(0 <= end < window for _, end, _ in offsets)
    good_completed = sum(0 <= end < window and row["valid_success"] for _, end, row in offsets)
    interval = 60 / rpm
    planned_count = int(phase.get("count") or 0)
    delays = [(start - (row["sequence"] - 1) * interval) * 1000 for start, _, row in offsets
              if isinstance(row["sequence"], int)]
    minutes = []
    for index in range(math.ceil(window / 60)):
        lower, upper = index * 60, min(window, (index + 1) * 60)
        planned = sum(lower <= seq * interval < upper for seq in range(planned_count))
        minutes.append({"minute": index + 1, "start_seconds": lower, "end_seconds": upper,
                        "planned_starts": planned,
                        "actual_starts": sum(lower <= start < upper for start, _, _ in offsets),
                        "completed": sum(lower <= end < upper for _, end, _ in offsets),
                        "successful_completed": sum(lower <= end < upper and row["valid_success"] for _, end, row in offsets)})
    starts = [start for start, _, _ in offsets]
    span = max(starts) - min(starts) if starts else 0
    cadence_ok = bool(starts) and min(delays, default=-math.inf) >= -interval * 1000
    cadence_ok = cadence_ok and span >= max(0, (planned_count - 2) * interval)
    count_consistent = abs(planned_count - rpm * window / 60) < 1e-6
    met = (count_consistent and len(offsets) == planned_count and abs(start_count - planned_count) <= 1
           and all(abs(row["actual_starts"] - row["planned_starts"]) <= 1 for row in minutes)
           and cadence_ok and max(starts, default=math.inf) <= window + interval)
    result.update({"actual_start_rpm": start_count * 60 / window, "completed_rpm_in_window": completed * 60 / window,
                   "successful_completed_rpm_in_window": good_completed * 60 / window,
                   "started_in_window": start_count, "completed_in_window": completed,
                   "successful_completed_in_window": good_completed,
                   "completed_after_window": sum(end >= window for _, end, _ in offsets),
                   "late_starts": sum(start >= window for start, _, _ in offsets), "minutes": minutes,
                   "schedule_delay_ms": distribution(delays), "start_span_seconds": span, "met": met})
    return result


def summarize_phase(directory: Path | str, phase: dict) -> dict:
    directory = Path(directory)
    issues, records = [], []
    paths = sorted(path for path in directory.rglob("supplier-requests.jsonl") if path.is_file() and not path.is_symlink())
    if len(paths) != 1:
        issues.append("REQUEST_EVIDENCE_MISSING_OR_AMBIGUOUS")
    else:
        try:
            for line in paths[0].read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row, valid = _normalize(json.loads(line))
                except ValueError:
                    row, valid = {}, False
                if not valid:
                    issues.append("INVALID_REQUEST_RECORD")
                if row:
                    records.append(row)
        except OSError:
            issues.append("UNREADABLE_REQUEST_EVIDENCE")
    expected = int(phase.get("count") or 0)
    if expected <= 0 or len(records) != expected:
        issues.append("REQUEST_COUNT_MISMATCH")
    if sorted(row["sequence"] for row in records if isinstance(row["sequence"], int)) != list(range(1, expected + 1)):
        issues.append("SEQUENCE_INCOMPLETE_OR_DUPLICATE")
    good = [row for row in records if row["valid_success"]]
    native, native_path = _load_one(directory, "benchmark_summary.json")
    if not isinstance(native, dict):
        issues.append("NATIVE_SUMMARY_MISSING_OR_AMBIGUOUS")
    elif any(native.get(key) != value for key, value in (
        ("Total Requests", len(records)), ("Success Requests", len(good)), ("Failed Requests", len(records) - len(good))
    )):
        issues.append("NATIVE_COUNTS_MISMATCH")
    rate = _rpm_summary(directory, phase, records)
    if rate and rate["met"] is None:
        issues.append("SCHEDULE_ORIGIN_MISSING")
    errors = Counter(row["error_code"] or "UNKNOWN_ERROR" for row in records if not row["valid_success"])
    return {"name": phase.get("name"), "planned_count": expected, "recorded_count": len(records),
            "valid_success": len(good), "failed": len(records) - len(good),
            "success_rate": len(good) / expected if expected > 0 else None,
            "evidence_status": "complete" if not issues else "incomplete", "issues": sorted(set(issues)),
            "ttft_seconds": distribution([row["ttft_seconds"] for row in good]),
            "ttfot_seconds": distribution([row["ttfot_seconds"] for row in good]),
            "latency_seconds": distribution([row["latency_seconds"] for row in good]),
            "errors": dict(sorted(errors.items())), "rate": rate, "records": records,
            "native_summary": native_path.relative_to(directory).as_posix() if native_path else None,
            "usage_known_requests": sum(row["usage_present"] for row in records),
            "prompt_tokens_reported": sum(row["prompt_tokens"] or 0 for row in records),
            "completion_tokens_reported": sum(row["completion_tokens"] or 0 for row in records)}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def build_report(output: Path | str) -> dict:
    output = Path(output).resolve()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    phases, issues, csv_rows = [], [], []
    runs = manifest.get("phase_runs") or []
    for phase in manifest.get("phases") or []:
        selected = [run for run in runs if run.get("name") == phase.get("name")]
        run = selected[0] if len(selected) == 1 else {}
        phase_path = output / str(run.get("run_dir") or phase.get("name"))
        if not phase_path.resolve().is_relative_to(output):
            raise ValueError("Phase evidence must remain within the supplier result directory")
        result = summarize_phase(phase_path, phase)
        result["run_status"] = run.get("status") or "not_run"
        result["exit_code"] = run.get("exit_code")
        result["run_dir"] = phase_path.relative_to(output).as_posix()
        native_html = sorted(path for path in phase_path.rglob("perf_report.html") if path.is_file())
        fallback = phase_path / "report.html"
        result["report"] = (native_html[0].relative_to(output).as_posix() if native_html else
                            fallback.relative_to(output).as_posix() if fallback.is_file() else None)
        if len(selected) != 1 or run.get("status") != "completed" or run.get("exit_code") != 0:
            issues.append(f"{phase.get('name')}:RUN_NOT_COMPLETED")
        if result["evidence_status"] != "complete":
            issues.append(f"{phase.get('name')}:EVIDENCE_INCOMPLETE")
        csv_rows.extend({"phase": phase.get("name"), **row} for row in result.pop("records"))
        phases.append(result)
    if not phases or len({p.get("name") for p in phases}) != len(phases):
        issues.append("PHASE_PLAN_INVALID")
    if manifest.get("engine") != {"name": "evalscope", "version": "1.13.0"}:
        issues.append("ENGINE_VERSION_UNEXPECTED")
    if manifest.get("planned_requests") != sum(p["planned_count"] for p in phases):
        issues.append("PLANNED_TOTAL_MISMATCH")
    thresholds = (manifest.get("target") or {}).get("thresholds") or {}
    configured = {key: finite(thresholds.get(key)) for key in (
        "min_success_rate", "ttft_p95_seconds", "ttfot_p95_seconds")}
    failures = []
    for phase in phases:
        if (phase["name"] == "connectivity" and phase["failed"]) or phase["valid_success"] == 0:
            failures.append(f"{phase['name']}:REQUEST_FAILURES")
        if phase["rate"] and phase["rate"]["met"] is False:
            failures.append(f"{phase['name']}:RPM_NOT_REACHED")
        for key, threshold in configured.items():
            if threshold is None:
                continue
            actual = (phase["success_rate"] if key == "min_success_rate" else
                      phase["ttft_seconds" if key == "ttft_p95_seconds" else "ttfot_seconds"]["p95"])
            if actual is None or (actual < threshold if key == "min_success_rate" else actual > threshold):
                failures.append(f"{phase['name']}:{key.upper()}_NOT_MET")
    assessment = ("incomplete" if issues else "not_passed" if failures else "passed" if any(v is not None for v in configured.values())
                  else "measurement_only")
    summary = {"schema_version": 1, "pack_version": manifest.get("pack_version"),
               "engine": manifest.get("engine"), "demo": manifest.get("demo") is True,
               "status": "failed" if issues else "completed", "complete": not issues, "assessment": assessment,
               "issues": sorted(set(issues)), "failures": failures,
               "planned_requests": manifest.get("planned_requests"),
               "recorded_requests": len(csv_rows), "valid_success": sum(p["valid_success"] for p in phases),
               "target": {key: (manifest.get("target") or {}).get(key) for key in (
                   "supplier", "model", "region", "reasoning_mode")},
               "thresholds": configured, "phases": phases,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    _write_json(output / "supplier-summary.json", summary)
    with (output / "requests.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)
    workbench = {"schema_version": 1, "run_id": output.name, "batch_file": "supplier-profile/v2",
                 "engine": "evalscope_perf", "status": summary["status"],
                 "conclusion": {"measurement_only": "needs_more_data", "passed": "approved", "not_passed": "rejected", "incomplete": "needs_more_data"}[assessment],
                 "assessment": assessment, "demo": summary["demo"], "results": [
                     {"provider": summary["target"]["supplier"], "model": summary["target"]["model"],
                      "modality": "text", "status": "success" if phase["evidence_status"] == "complete" and not phase["failed"] else "partial",
                      "success_rate": phase["success_rate"],
                      "latency_p95_ms": phase["latency_seconds"]["p95"] * 1000 if phase["latency_seconds"]["p95"] is not None else None,
                      "metrics": phase} for phase in phases]}
    _write_json(output / "workbench-summary.json", workbench)
    target = summary["target"]
    measurement = next((p for p in phases if p["name"] == "latency"), phases[0] if phases else {})
    rpm200 = next((p for p in phases if p["name"] == "rpm200"), {})
    title = "供应商专项测试报告" + (" · 本地模拟" if summary["demo"] else "")
    sections = [{"title": "本次结论", "body": ASSESSMENT_LABELS[assessment] + "。" +
                 ("这是本机模拟接口的工具验证，不能代表 GLM 5.3 或任何供应商性能。" if summary["demo"] else "") +
                 (" 发压条件未完成，不能仅凭此项归因于供应商。" if any("RPM_NOT_REACHED" in failure for failure in failures) else "") +
                 (" 未设 SLA 阈值：仅报告实测分布。" if not any(v is not None for v in configured.values()) else ""),
                 "tone": "bad" if assessment == "not_passed" else "warn" if assessment in ("measurement_only", "incomplete") else "good"},
                {"title": "首 Token 口径", "body": "TTFT 从客户端发起 HTTP 调用前计时，到首个非空思考或正文；TTFOT 到首个非空正文。包含连接池等待、DNS/TLS/建连，不包含发压排队；仅成功完整请求计入分位数，失败单独计数。与不含建连的工具口径不可直接比较。"},
                {"title": "RPM 与完成口径", "body": "RPM 是固定时间窗口内的客户端发起数，不是并发数或成功数。窗口从请求就绪时起算，包含进入官方调度器的开销，是原生调度起点的保守下界；绝不移动到首个请求。每分钟允许 1 次边界请求，须同时覆盖计划跨度。窗口结束后等待在途请求排空，尾部完成单列；发起记录不能证明服务端已收到。"},
                {"title": "完整成功", "body": "HTTP 200、非空正文、finish_reason=stop、[DONE] 均满足才算成功。只有思考、空回复、length 截断、断流、限流和超时均失败。未返回 usage 时 Token 用量未知，不估算为 0。"}]
    if issues or failures:
        sections.insert(1, {"title": "需处理项", "body": "；".join(issues + failures), "tone": "bad"})
    for phase in phases:
        if phase["rate"]:
            rate = phase["rate"]
            counts = "；".join(f"第 {m['minute']} 分钟：计划 {m['planned_starts']} / 发起 {m['actual_starts']} / 完成 {m['completed']} / 成功完成 {m['successful_completed']}" for m in rate["minutes"])
            sections.append({"title": f"{PHASE_LABELS.get(phase['name'], phase['name'])} 固定窗口核对",
                             "body": (counts or "缺少可核验的调度原点，无法证明固定窗口速率。") +
                             f"；窗后完成 {rate['completed_after_window'] if rate['completed_after_window'] is not None else '未知'} 次；调度滞后 P95 {number(rate['schedule_delay_ms']['p95'], ' ms')}。"})
    payload = {"schema_version": 1, "kind": "performance", "title": title,
               "subtitle": f"{target['supplier']} / {target['model']} · {target['region']} · EvalScope 1.13.0 · {target['reasoning_mode']}",
               "status": "rejected" if assessment == "not_passed" else "approved" if assessment == "passed" else "needs_more_data",
               "metrics": [{"label": "完整成功 / 计划", "value": f"{summary['valid_success']} / {summary['planned_requests']}", "note": f"记录 {len(csv_rows)} 次请求，包含失败"},
                           {"label": "首生成 P95", "value": number((measurement.get('ttft_seconds') or {}).get('p95'), ' s', 3), "note": "低负载，首思考或正文"},
                           {"label": "首正文 P95", "value": number((measurement.get('ttfot_seconds') or {}).get('p95'), ' s', 3), "note": "低负载，首个非空回答"},
                           {"label": "200 RPM 实际发起", "value": number((rpm200.get('rate') or {}).get('actual_start_rpm'), ' RPM', 2), "note": "固定窗口，不等于成功完成速率"}],
               "sections": sections,
               "columns": [{"key": "phase", "label": "阶段"}, {"key": "count", "label": "记录 / 计划"},
                           {"key": "success", "label": "完整成功率"}, {"key": "ttft", "label": "首生成 P95"},
                           {"key": "ttfot", "label": "首正文 P95"}, {"key": "rate", "label": "发起 RPM"},
                           {"key": "errors", "label": "失败类型"}, {"key": "evidence", "label": "证据完整性"},
                           {"key": "report", "label": "原生报告", "link": True}],
               "rows": [{"phase": PHASE_LABELS.get(p["name"], p["name"]),
                         "count": f"{p['recorded_count']} / {p['planned_count']}", "success": percent(p["success_rate"]),
                         "ttft": number(p["ttft_seconds"]["p95"], " s", 3), "ttfot": number(p["ttfot_seconds"]["p95"], " s", 3),
                         "rate": number((p["rate"] or {}).get("actual_start_rpm"), " RPM", 2),
                         "errors": "; ".join(f"{k}: {v}" for k, v in p["errors"].items()) or "无",
                         "evidence": "完整" if p["evidence_status"] == "complete" else "不完整", "report": p["report"]} for p in phases],
               "generated_at": summary["generated_at"]}
    _write_json(output / "report-data.json", payload)
    write_static_report(output / "report.html", payload)
    return summary
