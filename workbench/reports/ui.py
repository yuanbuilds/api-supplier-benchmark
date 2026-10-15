"""Render self-contained native HTML report pages from normalized benchmark data."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = Path(__file__).with_name("templates")


def json_for_script(payload: dict[str, Any]) -> str:
    """Serialize data safely for an inline script without changing the payload."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_static_html(payload: dict[str, Any], css: str, javascript: str) -> str:
    title = str(payload.get("title") or "Benchmark report")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{title}</title>
  <style>{css}</style>
</head>
<body>
  <script>window.__WORKBENCH_REPORT__={json_for_script(payload)};</script>
  <script>{javascript}</script>
</body>
</html>
"""


def write_static_report(output_path: Path, payload: dict[str, Any]) -> Path:
    """Write a report that opens directly from disk, with no server or CDN required."""
    quality_report = payload.get("kind") == "image_quality"
    css_path = TEMPLATE_DIR / ("image-quality-report.css" if quality_report else "report.css")
    js_path = TEMPLATE_DIR / ("image-quality-report.js" if quality_report else "report.js")
    links_path = TEMPLATE_DIR / "report-links.js"
    if not css_path.exists() or not js_path.exists() or not links_path.exists():
        raise RuntimeError("报告原生模板缺失：workbench/reports/templates/report.css 或 report.js。")
    javascript = "\n".join((js_path.read_text(encoding="utf-8"), links_path.read_text(encoding="utf-8")))
    output_path.write_text(render_static_html(payload, css_path.read_text(encoding="utf-8"), javascript), encoding="utf-8")
    return output_path


def percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "–"


def number(value: Any, suffix: str = "", precision: int = 1) -> str:
    try:
        return f"{float(value):.{precision}f}{suffix}"
    except (TypeError, ValueError):
        return "–"


STATUS_TEXT = {
    "approved": "已通过", "rejected": "未通过", "needs_more_data": "证据不足", "completed": "已完成",
    "failed": "失败", "passed": "通过", "success": "成功", "partial": "部分通过", "error": "错误",
    "fresh": "不使用缓存", "use-cache": "使用缓存",
}


def status_text(value: Any) -> str:
    text = str(value or "–")
    return STATUS_TEXT.get(text, text)


def image_compatibility_conclusion(summary: dict[str, Any], strict_rate: float) -> str:
    """Turn image transport and validation statistics into reader-facing text."""
    records = summary.get("records") or []
    requests = int(summary.get("requests") or len(records) or 0)
    strict_successes = sum(bool(record.get("strict_success")) for record in records)
    counts = summary.get("http_status_counts") or {}
    normalized_counts = {str(code): int(count) for code, count in counts.items()}

    if requests and strict_rate == 1 and normalized_counts == {"200": requests}:
        return f"已完成 {requests} 次请求：全部返回 HTTP 200，图片、尺寸和格式校验均通过。"

    http_text = "；".join(f"HTTP {code} {count} 次" for code, count in sorted(normalized_counts.items())) or "未记录 HTTP 响应"
    if requests:
        return f"已完成 {requests} 次请求，其中 {strict_successes} 次完整通过。接口响应：{http_text}。"
    return "暂无请求记录。"


def report_file_name(provider: Any, model: Any) -> str:
    safe = lambda value: re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("_") or "unknown"
    return f"evalscope__{safe(provider)}__{safe(model)}.html"


def materialize_evalscope_report(run_dir: Path, output_dir: Any, provider: Any, model: Any) -> str | None:
    """Copy EvalScope's self-contained page next to the workbench report.

    Some embedded file viewers deny navigation into deeply nested result paths,
    while a sibling file remains directly readable from the report directory.
    """
    if not output_dir:
        return None
    directory = Path(str(output_dir))
    if not directory.is_absolute():
        directory = ROOT / directory
    resolved_run_dir = run_dir.resolve()
    for candidate_dir in (directory, *directory.parents):
        candidates = sorted(candidate_dir.glob("perf_report.html"))
        if candidates:
            destination = resolved_run_dir / report_file_name(provider, model)
            if candidates[0].resolve() != destination.resolve():
                shutil.copy2(candidates[0], destination)
            return destination.name
        if candidate_dir.resolve() == resolved_run_dir:
            break
    return None


def comparison_image_name(provider: Any, model: Any, record: dict[str, Any], source: Path) -> str:
    """Create a sortable, collision-safe name for a run-level comparison image."""
    safe = lambda value: re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("_") or "unknown"
    case = safe(record.get("case") or "default")
    size = safe(record.get("size") or "unknown-size")
    try:
        sample = f"{int(record.get('index') or 1):02d}"
    except (TypeError, ValueError):
        sample = "01"
    return f"{case}__{size}__{sample}__{safe(provider)}__{safe(model)}{source.suffix.lower()}"


def materialize_image_comparison_set(run_dir: Path, summaries: list[dict[str, Any]]) -> list[str]:
    """Expose all returned images in one flat run-level folder for visual comparison.

    Target folders remain the canonical evidence location.  Hard links keep the
    comparison set from duplicating image bytes; copying is only a portability
    fallback when linking is unavailable.
    """
    comparison_dir = run_dir / "images"
    paths = []
    for summary in summaries:
        target = summary.get("target") or {}
        target_dir = str(target.get("report_directory") or "")
        for record in summary.get("records") or []:
            image_path = record.get("image_path")
            if not target_dir or not image_path:
                continue
            source = run_dir / target_dir / str(image_path)
            if not source.is_file():
                continue
            comparison_dir.mkdir(parents=True, exist_ok=True)
            destination = comparison_dir / comparison_image_name(target.get("provider"), target.get("model"), record, source)
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
            paths.append(str(destination.relative_to(run_dir)))
    return paths


def performance_report_data(run_dir: Path, manifest: dict[str, Any], rows: list[dict[str, Any]], admission: dict[str, Any] | None, totals: dict[str, Any]) -> dict[str, Any]:
    conclusion = str((admission or {}).get("conclusion") or "completed")
    status = conclusion if conclusion in {"approved", "rejected", "needs_more_data"} else "completed"
    display_rows = [
        {
            "provider": row.get("provider"),
            "model": row.get("model"),
            "modality": row.get("modality"),
            "status": row.get("status"),
            "success_rate": percent(row.get("success_rate")),
            "p50": number(row.get("e2e_p50_ms"), " ms"),
            "p95": number(row.get("e2e_p95_ms"), " ms"),
            "ttft_p95": number(row.get("ttft_p95_ms"), " ms"),
            "throughput": number(row.get("tokens_per_second"), " tok/s", 2),
            "planned_rpm": number(row.get("planned_requests_per_minute"), " RPM", 1),
            "actual_rpm": number(row.get("actual_requests_per_minute"), " RPM", 1),
            "requests": row.get("total_requests") or "–",
            "error": row.get("error") or "–",
            "evalscope_report": materialize_evalscope_report(run_dir, row.get("output_dir"), row.get("provider"), row.get("model")),
        }
        for row in rows
    ]
    admission_items = (admission or {}).get("results") or []
    sections = [
        {
            "title": "准入结论",
            "body": (" · ".join(f"{item.get('provider')}/{item.get('model')}: {status_text(item.get('decision'))}" for item in admission_items) or "未生成准入结论。"),
            "tone": "good" if status == "approved" else "bad" if status == "rejected" else "warn",
        }
    ]
    return {
        "schema_version": 1,
        "kind": "performance",
        "title": "性能评测报告",
        "subtitle": f"{manifest.get('run_id')} · {manifest.get('batch_name')} · EvalScope",
        "status": status,
        "metrics": [
            {"label": "模型数", "value": str(len(rows)), "note": "模型结果行"},
            {"label": "请求数", "value": str(totals.get("total_requests", 0)), "note": f"成功 {totals.get('success_requests', 0)} 次"},
            {"label": "平均成功率", "value": percent(totals.get("avg_success_rate")), "note": "按模型平均", "tone": "good" if totals.get("avg_success_rate") == 1 else "warn"},
            {"label": "端到端 P95 延迟", "value": number(totals.get("max_p95_latency_ms"), " ms"), "note": "各模型中的最大值"},
            {"label": "吞吐量", "value": number(totals.get("avg_tokens_per_second"), " tok/s", 2), "note": "平均输出"},
        ],
        "sections": sections,
        "columns": [
            {"key": "provider", "label": "供应商"}, {"key": "model", "label": "模型"}, {"key": "modality", "label": "模态"},
            {"key": "status", "label": "状态"}, {"key": "success_rate", "label": "成功率", "numeric": True},
            {"key": "p50", "label": "端到端 P50 延迟", "numeric": True}, {"key": "p95", "label": "端到端 P95 延迟", "numeric": True},
            {"key": "ttft_p95", "label": "首 Token P95 延迟", "numeric": True}, {"key": "throughput", "label": "吞吐量", "numeric": True},
            {"key": "planned_rpm", "label": "计划 RPM", "numeric": True}, {"key": "actual_rpm", "label": "实际完成 RPM", "numeric": True},
            {"key": "requests", "label": "请求数", "numeric": True}, {"key": "error", "label": "错误信息"}, {"key": "evalscope_report", "label": "EvalScope 原始报告", "link": True},
        ],
        "rows": display_rows,
        "generated_at": manifest.get("created_at"),
    }


def image_report_data(target: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    strict_rate = float(summary.get("strict_success_rate") or 0)
    status = "approved" if strict_rate == 1 else "rejected" if strict_rate == 0 else "needs_more_data"
    records = summary.get("records") or []
    plan = summary.get("plan") or {}
    rpm = plan.get("requests_per_minute")
    rate = summary.get("rate_control") or {}
    display_rows = [
        {
            "operation": "图生图" if record.get("operation") == "image_to_image" else "文生图",
            "request_size": record.get("size"),
            "actual_size": f"{record.get('actual_width')}x{record.get('actual_height')}" if record.get("actual_width") is not None else "–",
            "expected_format": record.get("expected_format") or "png",
            "actual_format": record.get("actual_format") or "–",
            "status": record.get("result_category") or ("passed" if record.get("strict_success") else "failed"),
            "reason": record.get("result_detail") or record.get("error") or "–",
            "latency": number(record.get("latency_seconds"), " s", 2),
            "request": {"endpoint": record.get("request_endpoint") or target.get("endpoint"), "body": record.get("request_body")},
            "provider_response": record.get("provider_response") or {},
        }
        for record in records
    ]
    return {
        "schema_version": 1,
        "kind": "image",
        "title": "生图兼容性报告",
        "subtitle": f"{target.get('provider')} / {target.get('model')} · {summary.get('plan', {}).get('operation', 'text_to_image')} · {summary.get('requests', 0)} 次请求",
        "status": status,
        "metrics": [
            {"label": "严格成功率", "value": percent(strict_rate), "note": "图片、尺寸和格式均通过", "tone": "good" if strict_rate == 1 else "bad" if strict_rate == 0 else "warn"},
            {"label": "有效图片率", "value": percent(summary.get("valid_image_rate")), "note": "可完整解码"},
            {"label": "尺寸匹配率", "value": percent(summary.get("dimension_match_rate")), "note": "实际尺寸完全一致"},
            {"label": "格式匹配率", "value": percent(summary.get("format_match_rate")), "note": "实际文件格式"},
            {"label": "P95 延迟", "value": number((summary.get("latency_seconds") or {}).get("p95"), " s", 2), "note": f"{summary.get('throughput_images_per_minute', '–')} 张/分钟"},
            {"label": "计划速率", "value": number(rpm, " RPM", 1) if rpm is not None else "不限速", "note": "请求发起速率；不改变总请求数"},
            {"label": "实际发起速率", "value": number(rate.get("actual_start_rpm"), " RPM", 1), "note": f"峰值在途 {rate.get('peak_in_flight', '–')} · 调度延迟 P95 {number((rate.get('schedule_delay_ms') or {}).get('p95'), ' ms', 0)}"} if rate else {"label": "实际发起速率", "value": "–", "note": "仅固定窗口 RPM 测试提供"},
        ],
        "sections": [{"title": "兼容性结论", "body": image_compatibility_conclusion(summary, strict_rate), "tone": "good" if strict_rate == 1 else "bad" if strict_rate == 0 else "warn"}],
        "columns": [
            {"key": "operation", "label": "操作"}, {"key": "request_size", "label": "请求尺寸"}, {"key": "actual_size", "label": "实际尺寸"},
            {"key": "expected_format", "label": "期望格式"}, {"key": "actual_format", "label": "实际格式"},
            {"key": "status", "label": "结果"}, {"key": "reason", "label": "原因"},
            {"key": "latency", "label": "耗时", "numeric": True}, {"key": "request", "label": "请求信息"}, {"key": "provider_response", "label": "供应商响应"},
        ],
        "rows": display_rows,
    }


def suite_report_data(run_id: str, entries: list[dict[str, Any]], admission: dict[str, Any], *, suite_dir: Path | None = None) -> dict[str, Any]:
    def report_link(entry: dict[str, Any]) -> str:
        report = entry.get("report")
        if not report:
            return "–"
        report_dir = Path(str(report))
        if suite_dir is None:
            return f"../{report_dir.name}/report.html"
        return os.path.relpath(report_dir / "report.html", suite_dir)

    conclusion = str(admission.get("conclusion") or "needs_more_data")
    rows = [
        {"flow": entry.get("flow"), "status": entry.get("status"), "decision": decision.get("decision", "–"), "reasons": "; ".join(decision.get("reasons") or []) or "–", "report": report_link(entry)}
        for entry, decision in zip(entries, admission.get("results") or [])
    ]
    return {
        "schema_version": 1, "kind": "suite", "title": "评测套件总览", "subtitle": f"{run_id} · 已选择 {len(entries)} 个评测流程", "status": conclusion,
        "metrics": [
            {"label": "评测流程", "value": str(len(entries)), "note": "已选择的评测"},
            {"label": "已通过", "value": str(sum(item.get("decision") == "approved" for item in admission.get("results") or [])), "note": "准入结论", "tone": "good"},
            {"label": "未通过", "value": str(sum(item.get("decision") == "rejected" for item in admission.get("results") or [])), "note": "需要处理", "tone": "bad"},
        ],
        "sections": [{"title": "套件准入结论", "body": f"{status_text(conclusion)} · 详细策略证据见 admission.json", "tone": "good" if conclusion == "approved" else "bad" if conclusion == "rejected" else "warn"}],
        "columns": [{"key": "flow", "label": "评测流程"}, {"key": "status", "label": "执行状态"}, {"key": "decision", "label": "准入结论"}, {"key": "reasons", "label": "原因"}, {"key": "report", "label": "报告", "link": True}],
        "rows": rows,
    }


def business_report_data(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = manifest.get("result_counts") or {}
    total = sum(int(counts.get(key) or 0) for key in ("passed", "failed", "error"))
    status = "approved" if not counts.get("failed") and not counts.get("error") else "rejected"
    provider_summary = []
    for provider in sorted({str(row.get("provider") or "unknown") for row in rows}):
        provider_rows = [row for row in rows if str(row.get("provider") or "unknown") == provider]
        passed = sum(row.get("status") == "passed" for row in provider_rows)
        failed = sum(row.get("status") == "failed" for row in provider_rows)
        errors = sum(row.get("status") == "error" for row in provider_rows)
        provider_summary.append(f"{provider}：通过 {passed}/{len(provider_rows)}，失败 {failed}，错误 {errors}")
    sections = [
        {"title": "业务结论", "body": f"共 {total} 条契约：通过 {counts.get('passed', 0)}，失败 {counts.get('failed', 0)}，错误 {counts.get('error', 0)}。", "tone": "good" if status == "approved" else "bad"},
        {"title": "供应商概览", "body": "；".join(provider_summary) or "暂无供应商结果。", "tone": "neutral"},
        {"title": "缓存策略", "body": f"{status_text(manifest.get('cache_policy'))} · 评测档位 {manifest.get('profile')}", "tone": "warn" if manifest.get("cache_policy") == "use-cache" else "neutral"},
    ]
    def score(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "–"

    def reason(row: dict[str, Any]) -> str:
        if row.get("status") == "passed":
            return "全部断言通过"
        raw = " ".join(str(row.get("reason") or "").split())
        missing = re.search(r"Expected output to contain all of \[([^]]+)\]\. Missing: \[([^]]+)\]", raw)
        if missing:
            return f"输出缺少期望内容：{missing.group(2)}（期望包含：{missing.group(1)}）"
        api_error = re.search(r"API error:\s*(\d{3})\s*([^\\{]+)", raw, re.IGNORECASE)
        if api_error:
            return f"接口请求失败：HTTP {api_error.group(1)} {api_error.group(2).strip()}"
        return raw or "未提供失败原因"

    return {
        "schema_version": 1, "kind": "business", "title": "业务契约报告", "subtitle": f"{manifest.get('run_id')} · {manifest.get('config')} · {manifest.get('cache_policy')}", "status": status,
        "metrics": [
            {"label": "用例数", "value": str(total), "note": "已评测断言"},
            {"label": "通过", "value": str(counts.get("passed", 0)), "note": "契约断言", "tone": "good"},
            {"label": "失败", "value": str(counts.get("failed", 0)), "note": "断言不符合预期", "tone": "bad" if counts.get("failed") else "good"},
            {"label": "错误", "value": str(counts.get("error", 0)), "note": "供应商或运行器错误", "tone": "bad" if counts.get("error") else "good"},
        ],
        "sections": sections,
        "columns": [{"key": "provider", "label": "供应商"}, {"key": "test_case", "label": "测试用例"}, {"key": "status", "label": "状态"}, {"key": "score", "label": "断言得分", "numeric": True}, {"key": "latency", "label": "耗时", "numeric": True}, {"key": "cached", "label": "使用缓存"}, {"key": "reason", "label": "结果说明"}],
        "rows": [{**row, "score": score(row.get("score")), "latency": number(row.get("latency_ms"), " ms"), "cached": "yes" if row.get("cached") else "no", "reason": reason(row)} for row in rows],
        "generated_at": manifest.get("generated_at"), "raw_report": "report-legacy.html",
    }


def image_quality_report_data(run_id: str, summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an evidence-first report for quality-regression image batches."""
    first_plan = (summaries[0].get("plan") or {}) if summaries else {}
    focus = str(first_plan.get("evaluation_focus") or "creative_quality")
    detail_comparison = focus == "effective_resolution" or str(first_plan.get("test_type") or "") == "resolution_detail_regression"
    canvas_integrity = focus == "canvas_integrity"
    checklist = [
        "实际像素严格等于请求尺寸，文件格式为 PNG。",
    ]
    if canvas_integrity:
        checklist.extend((
            "四边连续边框、四个角标和上下左右边缘物体均完整可见，没有裁切、补边、拉伸或空白带。",
            "中心瓶保持居中，边缘物体的位置关系符合请求比例；没有缺失、复制或融合。",
        ))
    else:
        checklist.extend((
            "主瓶与小瓶标签均为三行：LUMERA / VITAMIN C 15% / 30 mL；不得错字、拆行、重复或新增字符。",
            "最小瓶文字在 100% 原像素下可逐字辨认。",
            "手部、五根手指、滴管和商品关系自然；没有融合、缺指、重复物体或悬置物。",
            "主体、文字与配件均在安全边距内；玻璃、金属、液体、皮肤、丝绸、柑橘、反射和光照关系合理。",
        ))
    if detail_comparison:
        checklist.extend((
            "比较 1536、2560、3840 宽度档位的标签、发丝、水滴、玻璃、金属、丝绸与果肉细节；4K 必须带来稳定、可用的细节增益。",
            "若 4K 文件正确但没有细节增益，或出现大面积平滑、过度锐化、重复纹理等放大痕迹，标记为“4K 细节交付存疑”。",
        ))
    targets = []
    total_records = total_valid = 0
    for summary in summaries:
        target = summary.get("target") or {}
        directory = str(target.get("report_directory") or "")
        records = []
        for record in summary.get("records") or []:
            total_records += 1
            total_valid += bool(record.get("strict_success"))
            image_path = record.get("image_path")
            records.append({
                "image": f"{directory}/{image_path}" if directory and image_path else None,
                "size": record.get("size") or "未知尺寸",
                "actual_size": f"{record.get('actual_width')}x{record.get('actual_height')}" if record.get("actual_width") is not None else "–",
                "format": record.get("actual_format") or "–",
                "latency": number(record.get("latency_seconds"), " s", 2),
                "status": "自动校验通过" if record.get("strict_success") else "自动校验不通过",
                "reason": record.get("result_detail") or record.get("error") or "–",
                "sample": record.get("index") or 1,
            })
        targets.append({"provider": target.get("provider") or "unknown", "model": target.get("model") or "unknown", "records": records})
    return {
        "schema_version": 1,
        "kind": "image_quality",
        "title": "生图视觉验收报告",
        "subtitle": f"{run_id} · {'1K/2K/4K 细节对照' if detail_comparison else '全画布完整性' if canvas_integrity else '4K 电商主视觉内容验收'} · 需在 100% 原像素下人工复核",
        "status": "needs_more_data",
        "metrics": [
            {"label": "渠道数", "value": str(len(targets)), "note": "供应商 / 模型目标"},
            {"label": "图片数", "value": str(total_records), "note": f"自动严格通过 {total_valid}/{total_records}"},
            {"label": "自动校验", "value": percent(total_valid / total_records) if total_records else "–", "note": "仅图片、尺寸和格式；不代表视觉交付"},
            {"label": "人工结论", "value": "待复核", "note": "报告不自动判断文字、手部或真实细节增益", "tone": "warn"},
        ],
        "checklist": checklist,
        "targets": targets,
    }


def image_overview_report_data(run_id: str, summaries: list[dict[str, Any]]) -> dict[str, Any]:
    plans = [item.get("plan") or {} for item in summaries]
    test_types = {str(plan.get("test_type") or "") for plan in plans}
    visual_purposes = {str(plan.get("evaluation_purpose") or "") for plan in plans}
    if visual_purposes == {"visual"} or (test_types and test_types <= {"quality_regression", "resolution_detail_regression"}):
        return image_quality_report_data(run_id, summaries)
    rows = [
        {
            "provider": item.get("target", {}).get("provider"), "model": item.get("target", {}).get("model"),
            "operation": "图生图" if item.get("plan", {}).get("operation") == "image_to_image" else "文生图",
            "strict_success": percent(item.get("strict_success_rate")), "size_match": percent(item.get("dimension_match_rate")),
            "format_match": percent(item.get("format_match_rate")), "p95": number((item.get("latency_seconds") or {}).get("p95"), " s", 2),
            "throughput": number(item.get("throughput_images_per_minute"), " img/min", 2),
            "report": f"{item.get('target', {}).get('report_directory')}/report.html",
        }
        for item in summaries
    ]
    average = sum(float(item.get("strict_success_rate") or 0) for item in summaries) / len(summaries) if summaries else 0
    return {
        "schema_version": 1, "kind": "image", "title": "生图评测总览", "subtitle": f"{run_id} · {len(summaries)} 个模型目标", "status": "approved" if average == 1 else "rejected" if average == 0 else "needs_more_data",
        "metrics": [{"label": "目标数", "value": str(len(summaries)), "note": "供应商与模型路由"}, {"label": "平均严格成功率", "value": percent(average), "note": "按目标平均", "tone": "good" if average == 1 else "bad" if average == 0 else "warn"}],
        "columns": [{"key": "provider", "label": "供应商"}, {"key": "model", "label": "模型"}, {"key": "operation", "label": "操作"}, {"key": "strict_success", "label": "严格成功率", "numeric": True}, {"key": "size_match", "label": "尺寸匹配率", "numeric": True}, {"key": "format_match", "label": "格式匹配率", "numeric": True}, {"key": "p95", "label": "P95", "numeric": True}, {"key": "throughput", "label": "吞吐量", "numeric": True}, {"key": "report", "label": "报告", "link": True}],
        "rows": rows,
    }
