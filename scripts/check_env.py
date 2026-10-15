#!/usr/bin/env python3
"""Validate the actual providers and tools required by one or more benchmark batches."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.catalog import ROOT, batch_engine, load_dotenv, preflight_payload, tool_status, validate_batch
from workbench.config import load_yaml
from workbench.toolchain import PROMPTFOO_VERSION, promptfoo_executable, promptfoo_version


DEFAULT_BATCHES = ["evals/performance/evalscope/chat.yaml", "evals/performance/evalscope/vision.yaml", "evals/image/base/text-to-image.yaml", "evals/business/promptfoo/business-smoke.yaml"]
ENGINE_LABELS = {
    "evalscope_perf": "EvalScope 性能评测",
    "image_generation": "生图评测",
}


def promptfoo_preflight(path: Path) -> dict:
    """Validate a native Promptfoo config without forcing it into batch YAML."""
    source = path.read_text(encoding="utf-8")
    description_match = re.search(r"^description:\s*(.+)$", source, re.MULTILINE)
    providers_section = re.search(r"^providers:\s*$\n(.*?)(?=^[A-Za-z_][A-Za-z0-9_]*:\s*$|\Z)", source, re.MULTILINE | re.DOTALL)
    provider_blocks = re.split(r"^\s*-\s+id:\s*", providers_section.group(1) if providers_section else "", flags=re.MULTILINE)[1:]
    errors, targets = [], []
    if not provider_blocks:
        errors.append("Promptfoo 配置必须包含非空 providers 列表。")
    for block in provider_blocks:
        provider_id, _, fields = block.partition("\n")
        provider_id = provider_id.strip().strip("'\"")
        label_match = re.search(r"^\s*label:\s*(.+)$", fields, re.MULTILINE)
        key_match = re.search(r"^\s*apiKeyEnvar:\s*(.+)$", fields, re.MULTILINE)
        label = (label_match.group(1).strip().strip("'\"") if label_match else provider_id) or "unknown"
        targets.append({"provider": label, "model": provider_id or label})
        key_name = key_match.group(1).strip().strip("'\"") if key_match else ""
        if not key_name:
            errors.append(f"{label} 缺少 config.apiKeyEnvar。")
        elif not os.environ.get(key_name):
            errors.append(f"{label} 缺少环境变量：{key_name}")
    executable = promptfoo_executable()
    version = ""
    if executable:
        version = promptfoo_version(executable) or "unknown"
        if version != PROMPTFOO_VERSION:
            errors.append(f"promptfoo 版本为 {version}；项目要求 {PROMPTFOO_VERSION}。")
    else:
        errors.append(f"缺少运行工具：promptfoo（请在项目目录执行 npm ci，安装本地锁定版本 {PROMPTFOO_VERSION}）")
    return {"schema_version": 1, "batch_file": str(path.relative_to(ROOT)), "batch_name": description_match.group(1).strip() if description_match else path.stem, "engine": "promptfoo", "tool": "promptfoo", "tool_ready": bool(executable), "tool_path": executable, "tool_version": version, "targets": targets, "ready": not errors, "errors": errors, "warnings": []}


def print_tool_status(engine: str) -> bool:
    """Show an actionable tool diagnostic for the selected benchmark engine."""
    status = tool_status(engine)
    tool, executable = status["tool"], status["tool_path"]
    print(f"  引擎: {ENGINE_LABELS.get(engine, engine)}")
    if executable:
        print(f"  工具: {tool} ✓ ({executable})")
        return True
    print(f"  工具: {tool} ✗ 未找到")
    if tool == "evalscope":
        print("  原因: 当前 shell 未激活安装了 EvalScope 的 Python/Conda/venv 环境。")
        print("  处理: 先执行 conda activate evalscope（或激活你的项目 venv），再重新运行本命令。")
        print("  安装: 若尚未安装，可在该环境执行 python -m pip install evalscope。")
        print("  验证: command -v evalscope && evalscope --help")
    return False


@contextmanager
def batch_environment(path: Path, is_promptfoo: bool = False):
    """Scope legacy dotenv keys to one batch, never a subsequent supplier run."""
    test = {} if is_promptfoo else load_yaml(path).get("test")
    supplier = isinstance(test, dict) and test.get("supplier_acceptance") is True
    original_keys = set(os.environ)
    try:
        if not supplier:
            load_dotenv()
        yield
    finally:
        for key in set(os.environ) - original_keys:
            os.environ.pop(key, None)


def inspect_batch(raw_path: str, json_output: bool) -> bool:
    path = (ROOT / raw_path).resolve()
    is_promptfoo = path.parts[-3:-1] == ("business", "promptfoo")
    try:
        with batch_environment(path, is_promptfoo):
            if is_promptfoo:
                payload = promptfoo_preflight(path)
            elif json_output:
                payload = preflight_payload(path)
            else:
                batch, errors, warnings = validate_batch(path)
                engine = batch_engine(batch)
                print(f"\n{raw_path}")
                tool_ready = print_tool_status(engine)
                for warning in warnings:
                    print(f"  WARN {warning}")
                for error in errors:
                    print(f"  ERROR {error}")
                return bool(errors) or not tool_ready
            if json_output:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"\n{raw_path}")
                print("  引擎: Promptfoo 业务契约")
                print(f"  工具: promptfoo {'✓' if payload.get('tool_ready') else '✗ 未找到'}" + (f" ({payload.get('tool_version')})" if payload.get('tool_version') else ""))
                for error in payload["errors"]:
                    print(f"  ERROR {error}")
                for target in payload.get("targets", []):
                    print(f"  目标: {target['provider']}")
            return not payload["ready"]
    except (OSError, ValueError) as error:
        if json_output:
            print(json.dumps({"schema_version": 1, "batch_file": raw_path, "ready": False,
                              "errors": [str(error)], "warnings": []}, ensure_ascii=False))
        else:
            print(f"\n{raw_path}\n  ERROR {error}")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the environment for selected benchmark batch files.")
    parser.add_argument("batch_files", nargs="*", help="Batch files to inspect; defaults to current chat, vision, and image batches.")
    parser.add_argument("--json", action="store_true", help="Emit stable machine-readable preflight data for a future UI or integration.")
    args = parser.parse_args()
    failures = [inspect_batch(path, args.json) for path in args.batch_files or DEFAULT_BATCHES]
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
