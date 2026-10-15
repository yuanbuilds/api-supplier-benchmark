"""Shared catalog and batch-validation rules for the benchmark workbench."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.config import load_dotenv, load_yaml, provider_catalog, provider_connection, provider_config_path


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_PATH = ROOT / "configs" / "providers.yaml"
GUARDRAILS_PATH = ROOT / "configs" / "guardrails.yaml"
ENGINE_TO_TOOL = {
    "evalscope_perf": "evalscope",
    "image_generation": "python3",
}


def load_catalogs() -> dict:
    path = provider_config_path() if os.environ.get("WORKBENCH_PROVIDERS_FILE") else PROVIDERS_PATH
    return provider_catalog(load_yaml(path))


def batch_engine(batch: dict) -> str:
    engine = batch.get("engine") or batch.get("test", {}).get("engine")
    if engine not in {"evalscope_perf", "image_generation"}:
        raise ValueError("批次必须声明 engine: evalscope_perf 或 image_generation。")
    return engine


def required_tool(engine: str) -> str:
    return ENGINE_TO_TOOL[engine]


def tool_status(engine: str) -> dict:
    tool = required_tool(engine)
    executable = shutil.which(tool)
    return {"tool": tool, "tool_ready": bool(executable), "tool_path": executable}


def positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else [value]


def image_sizes(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def validate_batch(batch_path: Path, allow_cost_overrun: bool = False) -> tuple[dict, list[str], list[str]]:
    """Return parsed batch plus blocking connection/environment errors."""
    errors, warnings = [], []
    try:
        batch = load_yaml(batch_path)
    except (OSError, ValueError) as error:
        return {}, [f"批次无法解析：{error}"], warnings
    if not isinstance(batch, dict):
        return {}, ["批次必须是 YAML 对象。"], warnings
    engine = batch_engine(batch)
    providers = load_catalogs()
    if batch.get("schema_version") != 1:
        errors.append("批次必须声明 schema_version: 1。")
    if not isinstance(batch.get("name"), str) or not batch["name"].strip():
        errors.append("批次必须声明非空 name。")
    targets = batch.get("targets")
    if not isinstance(targets, dict) or not targets:
        errors.append("批次没有 targets。")
        targets = {}
    for provider_name, target_models in targets.items():
        if not isinstance(target_models, list) or not target_models or not all(isinstance(model, str) and model.strip() for model in target_models):
            errors.append(f"{provider_name} 的 targets 必须是非空模型列表。")
            continue
        provider = providers.get(provider_name)
        if provider is None:
            errors.append(f"供应商未登记：{provider_name}")
            continue
        key_name = provider_connection(provider).get("api_key_env")
        if key_name and not os.environ.get(key_name):
            errors.append(f"{provider_name} 缺少环境变量：{key_name}")
        if provider.get("type") == "mixed" and any(model.lower().startswith("gemini-") for model in target_models):
            if not isinstance(provider.get("gemini_base_url"), str) or not provider["gemini_base_url"].strip():
                errors.append(f"{provider_name} 的 Gemini 模型需要配置 gemini_base_url。")
    test = batch.get("test")
    if not isinstance(test, dict):
        errors.append("批次必须声明 test 对象。")
        return batch, errors, warnings
    engine = batch_engine(batch)
    if engine == "evalscope_perf":
        if test.get("modality") not in {"text", "multimodal"}:
            errors.append("EvalScope 批次的 test.modality 必须为 text 或 multimodal。")
        if not isinstance(test.get("dataset"), str) or not test["dataset"].strip():
            errors.append("EvalScope 批次必须声明 test.dataset。")
        for field in ("concurrency", "number"):
            values = as_list(test.get(field))
            if not values or not all(positive_int(value) for value in values):
                errors.append(f"test.{field} 必须是正整数或正整数列表。")
        for field in ("timeout_seconds", "max_tokens", "rate"):
            if field in test and (not isinstance(test[field], (int, float)) or isinstance(test[field], bool) or test[field] <= 0):
                errors.append(f"test.{field} 必须为正数。")
        rpm = test.get("requests_per_minute")
        if rpm is not None:
            duration = test.get("duration_seconds")
            if not isinstance(rpm, (int, float)) or isinstance(rpm, bool) or rpm <= 0:
                errors.append("test.requests_per_minute 必须为正数。")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                errors.append("对话 RPM 测试必须声明正数 test.duration_seconds。")
            elif isinstance(rpm, (int, float)) and not isinstance(rpm, bool):
                expected = float(rpm) * float(duration) / 60
                numbers = as_list(test.get("number"))
                if not expected.is_integer() or any(value != int(expected) for value in numbers):
                    errors.append("对话 RPM 测试的 test.number 必须等于 requests_per_minute × duration_seconds / 60。")
    else:
        sizes = image_sizes(test.get("sizes"))
        if not isinstance(test.get("prompt"), str) or not test["prompt"].strip():
            errors.append("生图批次必须声明非空 test.prompt。")
        if not sizes or any(not re.fullmatch(r"[1-9]\d*x[1-9]\d*", size) for size in sizes):
            errors.append("test.sizes 必须包含形如 1024x1024 的尺寸。")
        for field in ("images_per_size", "concurrency"):
            if not positive_int(test.get(field)):
                errors.append(f"test.{field} 必须是正整数。")
        operation = test.get("operation", "text_to_image")
        if operation not in {"text_to_image", "image_to_image"}:
            errors.append("test.operation 必须为 text_to_image 或 image_to_image。")
        purpose = test.get("evaluation_purpose")
        if purpose is not None and purpose not in {"contract", "rate", "visual"}:
            errors.append("test.evaluation_purpose 必须为 contract、rate 或 visual。")
        focus = test.get("evaluation_focus")
        if focus is not None and purpose != "visual":
            errors.append("test.evaluation_focus 仅可用于 evaluation_purpose: visual。")
        response_format = test.get("response_format")
        if response_format is not None and response_format not in {"url", "b64_json"}:
            errors.append("test.response_format 必须为 url 或 b64_json。")
        if operation == "image_to_image":
            source = test.get("input_image")
            if not isinstance(source, str) or not source.strip():
                errors.append("图生图批次必须声明非空 test.input_image。")
            else:
                if not re.fullmatch(r"https?://[^\s]+", source):
                    source_path = Path(source)
                    if not source_path.is_absolute():
                        source_path = batch_path.parent / source_path
                    if not source_path.is_file():
                        errors.append(f"图生图输入图片不存在或不是文件：{source_path}")
        rpm = test.get("requests_per_minute")
        if rpm is not None and (not isinstance(rpm, (int, float)) or isinstance(rpm, bool) or rpm <= 0):
            errors.append("test.requests_per_minute 必须为正数。")
        if rpm is not None and test.get("duration_seconds") is not None:
            duration = test.get("duration_seconds")
            expected = float(rpm) * float(duration) / 60 if isinstance(rpm, (int, float)) and isinstance(duration, (int, float)) else 0
            requests = len(sizes) * test.get("images_per_size", 0)
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                errors.append("图片 RPM 测试必须声明正数 test.duration_seconds。")
            elif not expected.is_integer() or requests != int(expected):
                errors.append("图片 RPM 测试的 sizes × images_per_size 必须等于 requests_per_minute × duration_seconds / 60。")
            if test.get("retry_attempts", 0) != 0:
                errors.append("图片 RPM 测试必须设置 retry_attempts: 0，避免重试突破计划速率。")
        guardrails = load_yaml(GUARDRAILS_PATH).get("default", {})
        requests = len(sizes) * test.get("images_per_size", 0)
        if requests > guardrails.get("max_requests_per_target", float("inf")):
            errors.append(f"单个目标请求数 {requests} 超过请求上限 {guardrails['max_requests_per_target']}。")
        if test.get("concurrency", 0) > guardrails.get("max_concurrency", float("inf")):
            errors.append(f"并发 {test['concurrency']} 超过并发上限 {guardrails['max_concurrency']}。")
        overrides = batch.get("target_overrides", {})
        for provider_name, models in targets.items():
            for model in models if isinstance(models, list) else []:
                cost = overrides.get(provider_name, {}).get(model, {}).get("estimated_cost_per_image_usd", test.get("estimated_cost_per_image_usd"))
                if cost is not None:
                    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
                        errors.append(f"{provider_name}/{model} 的 estimated_cost_per_image_usd 必须为非负数。")
                    elif cost * requests > guardrails.get("max_estimated_cost_usd", float("inf")):
                        message = f"{provider_name}/{model} 预计成本 {cost * requests:.2f} USD 超过成本上限 {guardrails['max_estimated_cost_usd']} USD；请使用 --confirm-cost 明确确认。"
                        (warnings if allow_cost_overrun else errors).append(message)
    return batch, errors, warnings


def preflight_payload(batch_path: Path) -> dict:
    """Stable, UI-ready validation data with no terminal formatting concerns."""
    batch, errors, warnings = validate_batch(batch_path)
    engine = batch_engine(batch)
    tool = tool_status(engine)
    if not tool["tool_ready"]:
        errors = [*errors, f"缺少运行工具：{tool['tool']}"]
    return {
        "schema_version": 1,
        "batch_file": str(batch_path.relative_to(ROOT)),
        "batch_name": batch.get("name"),
        "engine": engine,
        **tool,
        "targets": [
            {"provider": provider, "model": model}
            for provider, models in batch.get("targets", {}).items()
            for model in models
        ],
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
    }
