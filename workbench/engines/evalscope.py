#!/usr/bin/env python3
"""Run EvalScope performance batches for multiple providers and models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from workbench.config import load_dotenv, load_yaml, provider_catalog, provider_connection, provider_config_path
from workbench.runs import create_run_directory, new_run_id, write_engine_handoff


ROOT = Path(__file__).resolve().parents[2]
SENSITIVE_VALUE_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9._~+/=-]+")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_version(command: list[str]) -> str:
    try:
        output = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    except OSError:
        return "not installed"
    return output.stdout.strip().splitlines()[0] if output.stdout.strip() else "unknown"


def redact_text(value: str) -> str:
    return SENSITIVE_VALUE_PATTERN.sub(lambda match: "Bearer ***" if match.group().startswith("Bearer ") else "sk-***", value)


def redact_json(value, key: str | None = None):
    sensitive_keys = {"authorization", "api_key", "apikey", "api-key", "access_token", "access-token"}
    if key and key.lower() in sensitive_keys:
        return "***"
    if isinstance(value, dict):
        return {item_key: redact_json(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def sanitize_run_artifacts(output_dir: Path) -> None:
    """Remove credentials from EvalScope artifacts before they are retained."""
    for path in output_dir.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        path.write_text(json.dumps(redact_json(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for pattern in ("*.log", "*.txt"):
        for path in output_dir.rglob(pattern):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            path.write_text(redact_text(content), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a batch file with EvalScope perf.",
    )
    parser.add_argument("batch_file", help="Batch config, for example evals/performance/evalscope/chat.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the manifest and EvalScope commands only. EvalScope will not run.",
    )
    parser.add_argument("--result-file", help="Write the created run directory for the public runner.")
    return parser.parse_args()


def normalize_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    parts = urlsplit(base)
    if parts.scheme and parts.netloc:
        normalized_path = re.sub(r"/+", "/", parts.path)
        base = urlunsplit((parts.scheme, parts.netloc, normalized_path.rstrip("/"), parts.query, parts.fragment))
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def interface_endpoint(base_url: str, interface: dict | None, default_path: str) -> str:
    """Use standard OpenAI-compatible endpoints unless a provider declares an override."""
    override = (interface or {}).get("endpoint") or (interface or {}).get("path")
    if isinstance(override, str) and override.startswith(("http://", "https://")):
        return override
    if isinstance(override, str):
        return f"{base_url.rstrip('/')}/{override.lstrip('/')}"
    return f"{base_url.rstrip('/')}/{default_path.lstrip('/')}"


def as_list(value, default: list[int] | None = None) -> list:
    if value is None:
        return default or []
    if isinstance(value, list):
        return value
    return [value]


def supplier_worker_environment(env: dict[str, str], api_key_env: str | None) -> dict[str, str]:
    """Minimal direct-network environment; no other provider credentials/proxies."""
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "HOME"}
    child = {key: value for key, value in env.items() if key in keep}
    child.update({"PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1", "WORKBENCH_SKIP_DOTENV": "1",
                  "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                  "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                  "NO_PROXY": "*", "no_proxy": "*", "NETRC": os.devnull, "TOKENIZERS_PARALLELISM": "false"})
    if api_key_env and env.get(api_key_env):
        child["WORKBENCH_SUPPLIER_KEY"] = env[api_key_env]
    return child


def supplier_test_parameters(test: dict) -> tuple[int, float | None]:
    if test.get("modality") != "text" or test.get("stream") is not True:
        raise ValueError("supplier_acceptance requires modality:text and stream:true")
    if test.get("dataset") != "line_by_line" or not test.get("dataset_path"):
        raise ValueError("supplier_acceptance requires local line_by_line dataset_path")
    if test.get("tokenizer_path"):
        raise ValueError("supplier_acceptance uses server usage; tokenizer download is disabled")
    counts = as_list(test.get("number"))
    if len(counts) != 1 or not isinstance(counts[0], int) or isinstance(counts[0], bool) or counts[0] < 1:
        raise ValueError("supplier_acceptance requires a single positive request number")
    workload = test.get("supplier_workload", "latency")
    if workload not in ("latency", "concurrency"):
        raise ValueError("supplier_workload must be latency or concurrency")
    rpm = test.get("requests_per_minute")
    if workload == "concurrency":
        if rpm is not None or test.get("open_loop") is True:
            raise ValueError("supplier concurrency workload is closed-loop and cannot use RPM or open_loop")
        parallel = as_list(test.get("concurrency"))
        if len(parallel) != 1 or type(parallel[0]) is not int or parallel[0] < 1:
            raise ValueError("supplier concurrency workload requires one explicit positive integer concurrency")
    if rpm is not None:
        duration = test.get("duration_seconds")
        if not isinstance(rpm, (int, float)) or not math.isfinite(rpm) or rpm <= 0:
            raise ValueError("requests_per_minute must be positive and finite")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration_seconds must be positive and finite")
        if not math.isclose(counts[0], rpm * duration / 60, abs_tol=1e-9):
            raise ValueError("number must equal RPM * duration_seconds / 60")
    elif workload == "latency" and as_list(test.get("concurrency"), [1]) != [1]:
        raise ValueError("supplier latency phase must use concurrency:[1]")
    return counts[0], rpm


def prepare_supplier_config(test: dict, item: dict, output_dir: Path) -> Path:
    """Compile local bodies into official replay input, never custom scheduling."""
    count, rpm = supplier_test_parameters(test)
    source = Path(test["dataset_path"])
    if not source.is_absolute():
        source = ROOT / source
    if not source.is_file():
        raise ValueError("Supplier dataset_path must refer to an existing local file")
    bodies = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not bodies or not all(isinstance(body, dict) and isinstance(body.get("messages"), list) for body in bodies):
        raise ValueError("Supplier JSONL lines must be complete JSON request bodies with messages")
    for body in bodies:
        forbidden = {"api_key", "authorization", "headers", "tools", "tool_choice", "function_call", "functions", "ignore_eos"}
        if any(key.lower() in forbidden or key.startswith("__") for key in body):
            raise ValueError("Supplier dataset contains forbidden transport/tool fields")
        if body.get("n", 1) != 1:
            raise ValueError("Supplier dataset requires n=1")
    dataset = output_dir / ("workload-trace.jsonl" if rpm else "request-bodies.jsonl")
    with dataset.open("w", encoding="utf-8") as handle:
        for index in range(count):
            body = dict(bodies[index % len(bodies)])
            request_id = f"request-{index + 1:06d}"
            body.update({"model": item["model"], "stream": True, "max_tokens": test.get("max_tokens", 4096),
                         "stream_options": {"include_usage": True}, "_workbench_request_id": request_id})
            line = {"timestamp": index * 60 / rpm, "request_id": request_id, "body": body} if rpm else body
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    timeout = test.get("timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout_seconds must be positive and finite")
    kwargs = {"url": item["url"], "api": "workbench_strict_supplier", "model": item["model"],
              "dataset": "workbench_supplier_trace" if rpm else "line_by_line", "dataset_path": str(dataset),
              "parallel": [0] if rpm else as_list(test.get("concurrency"), [1]), "number": [count], "rate": [rpm / 60] if rpm else [-1],
              "open_loop": bool(rpm), "stream": True, "max_tokens": test.get("max_tokens", 4096),
              "no_test_connection": True, "warmup_num": 0, "total_timeout": timeout,
              "connect_timeout": min(timeout, 10), "read_timeout": timeout, "outputs_dir": str(output_dir / "native"),
              "name": "supplier-acceptance", "debug": False, "seed": None}
    config = {"schema_version": 1, "arguments": kwargs, "requests_per_minute": rpm,
              "supplier_workload": test.get("supplier_workload", "latency"),
              "duration_seconds": test.get("duration_seconds"), "planned_requests": count,
              "records_file": str(output_dir / "supplier-requests.jsonl"),
              "schedule_file": str(output_dir / "supplier-schedule.json"),
              "source_dataset_sha256": sha256_file(source), "compiled_dataset_sha256": sha256_file(dataset),
              "metric_origin": "immediately before HTTP post; includes connection, DNS and TLS; excludes scheduler delay",
              "network": "direct; TLS verification enabled; redirects/retries disabled"}
    path = output_dir / "supplier-worker.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_supplier_command(command: list[str], env: dict[str, str], api_key_env: str | None, output_dir: Path, log_path: Path) -> int:
    child_env = supplier_worker_environment(env, api_key_env)
    secret = child_env.get("WORKBENCH_SUPPLIER_KEY", "")
    process = subprocess.Popen(command, cwd=output_dir, env=child_env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8") as handle:
        for line in process.stdout:
            handle.write(redact_text(line.replace(secret, "***") if secret else line))
    process.stdout.close()
    return process.wait()


def build_commands(batch: dict, providers: dict) -> list[dict]:
    test = batch.get("test", {})
    engine = test.get("engine", "evalscope_perf")
    if engine != "evalscope_perf":
        raise ValueError(f"Unsupported test engine: {engine}")

    strict_supplier = test.get("supplier_acceptance") is True
    if strict_supplier:
        supplier_test_parameters(test)
    modality = test.get("modality", "text")
    dataset = test.get("dataset", "openqa")
    dataset_path = test.get("dataset_path")
    tokenizer_path = test.get("tokenizer_path")
    parallel = as_list(test.get("concurrency"), [1])
    number = as_list(test.get("number"), [10])

    if dataset == "random_vl" and not tokenizer_path:
        raise ValueError(
            "EvalScope dataset random_vl requires test.tokenizer_path. "
            "Set it to a local tokenizer directory or a model id supported by your EvalScope environment."
        )

    if len(number) == 1 and len(parallel) > 1:
        number = number * len(parallel)
    if len(parallel) != len(number):
        raise ValueError("test.concurrency and test.number must have the same length")

    providers = provider_catalog(providers)
    commands = []
    for provider_name, models in batch.get("targets", {}).items():
        provider = providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Provider not found in configs/providers.yaml: {provider_name}")

        connection = provider_connection(provider)
        api_key_env = connection.get("api_key_env")
        output_url = normalize_url(interface_endpoint(connection["base_url"], provider.get("interfaces", {}).get("chat"), "/chat/completions"))

        for model in models:
            if strict_supplier:
                commands.append({"provider": provider_name, "model": model, "api_key_env": api_key_env,
                                 "modality": modality, "dataset": dataset_path, "tokenizer_path": None,
                                 "image_width": None, "image_height": None, "image_num": None,
                                 "parallel": [0] if test.get("requests_per_minute") else parallel, "number": number,
                                 "supplier_workload": test.get("supplier_workload", "latency"),
                                 "planned_requests_per_minute": test.get("requests_per_minute"),
                                 "duration_seconds": test.get("duration_seconds"), "supplier_acceptance": True,
                                 "url": output_url,
                                 "command_template": [sys.executable, str(ROOT / "workbench/engines/supplier_worker.py"), "__SUPPLIER_CONFIG__"]})
                continue
            cmd = [
                "evalscope",
                "perf",
                "--url",
                output_url,
                "--api",
                "openai",
                "--model",
                str(model),
                "--parallel",
                *[str(item) for item in parallel],
                "--number",
                *[str(item) for item in number],
                "--dataset",
                str(dataset),
                "--outputs-dir",
                "__OUTPUT_DIR__",
            ]

            rate = test.get("rate")
            planned_rpm = test.get("requests_per_minute")
            if planned_rpm is not None:
                rate = float(planned_rpm) / 60
            if rate is not None:
                cmd.extend(["--rate", str(rate)])
            if test.get("open_loop") is True or planned_rpm is not None:
                cmd.append("--open-loop")
            if test.get("duration") is not None:
                cmd.extend(["--duration", str(test["duration"])])
            if test.get("warmup_num") is not None:
                cmd.extend(["--warmup-num", str(test["warmup_num"])])
            if dataset_path:
                cmd.extend(["--dataset-path", str(dataset_path)])
            if tokenizer_path:
                cmd.extend(["--tokenizer-path", str(tokenizer_path)])
            if test.get("data_source") is not None:
                cmd.extend(["--data-source", str(test["data_source"])])
            if test.get("max_prompt_length") is not None:
                cmd.extend(["--max-prompt-length", str(test["max_prompt_length"])])
            if test.get("min_prompt_length") is not None:
                cmd.extend(["--min-prompt-length", str(test["min_prompt_length"])])
            if test.get("prompt") is not None:
                cmd.extend(["--prompt", str(test["prompt"])])
            if test.get("query_template") is not None:
                cmd.extend(["--query-template", str(test["query_template"])])
            if test.get("apply_chat_template") is not None:
                cmd.extend(["--apply-chat-template", str(test["apply_chat_template"]).lower()])
            if test.get("image_width") is not None:
                cmd.extend(["--image-width", str(test["image_width"])])
            if test.get("image_height") is not None:
                cmd.extend(["--image-height", str(test["image_height"])])
            if test.get("image_format") is not None:
                cmd.extend(["--image-format", str(test["image_format"])])
            if test.get("image_num") is not None:
                cmd.extend(["--image-num", str(test["image_num"])])
            if test.get("image_patch_size") is not None:
                cmd.extend(["--image-patch-size", str(test["image_patch_size"])])
            if test.get("max_tokens") is not None:
                cmd.extend(["--max-tokens", str(test["max_tokens"])])
            if test.get("temperature") is not None:
                cmd.extend(["--temperature", str(test["temperature"])])
            if test.get("top_p") is not None:
                cmd.extend(["--top-p", str(test["top_p"])])
            if test.get("top_k") is not None:
                cmd.extend(["--top-k", str(test["top_k"])])
            if test.get("timeout_seconds") is not None:
                cmd.extend(["--read-timeout", str(test["timeout_seconds"])])
            if test.get("stream") is True:
                cmd.append("--stream")
            elif test.get("stream") is False:
                cmd.append("--no-stream")
            if api_key_env:
                cmd.extend(["--headers", f"Authorization=Bearer ${{{api_key_env}}}"])

            commands.append(
                {
                    "provider": provider_name,
                    "model": model,
                    "api_key_env": api_key_env,
                    "modality": modality,
                    "dataset": dataset_path or dataset,
                    "tokenizer_path": tokenizer_path,
                    "image_width": test.get("image_width"),
                    "image_height": test.get("image_height"),
                    "image_num": test.get("image_num"),
                    "parallel": parallel,
                    "number": number,
                    "planned_requests_per_minute": planned_rpm,
                    "command_template": cmd,
                }
            )

    return commands


def command_for_storage(command: list[str]) -> list[str]:
    return [
        item if item.startswith("Authorization=Bearer ${") else "Authorization=Bearer ***"
        if item.startswith("Authorization=Bearer ")
        else item
        for item in command
    ]


def command_for_display(command: list[str]) -> list[str]:
    return [
        "Authorization=Bearer ***" if item.startswith("Authorization=Bearer ") else item
        for item in command
    ]


def printable_command(command: list[str]) -> str:
    return " ".join(command_for_display(command))


def command_for_execution(command: list[str], env: dict[str, str]) -> list[str]:
    executable_command = []
    for item in command:
        if item.startswith("Authorization=Bearer ${") and item.endswith("}"):
            key = item.removeprefix("Authorization=Bearer ${").removesuffix("}")
            executable_command.append(f"Authorization=Bearer {env[key]}")
        else:
            executable_command.append(item)
    return executable_command


def run_command(command: list[str], env: dict[str, str], log_path: Path) -> int:
    """Run EvalScope quietly and retain a redacted raw log for diagnosis."""
    executable_command = command_for_execution(command, env)
    process = subprocess.Popen(
        executable_command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8") as log_file:
        while chunk := process.stdout.read(4096):
            log_file.write(redact_text(chunk))
    process.stdout.close()
    return process.wait()


def display_value(value) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def failure_reason(log_path: Path) -> str:
    """Extract one safe, actionable root cause from EvalScope's verbose output."""
    try:
        content = ANSI_ESCAPE_PATTERN.sub("", log_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "未能读取详细日志。"
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in lines:
        if "Non-retryable error" not in line and "HTTP " not in line:
            continue
        status = re.search(r"HTTP\s+(\d+)", line)
        message = re.search(r'"message"\s*:\s*"([^"]+)"', line)
        if status and message:
            return redact_text(f"HTTP {status.group(1)}：{message.group(1)}")
        return redact_text(line[:300])
    for line in lines:
        if "ERROR" in line or line.startswith(("ValueError:", "TimeoutError:", "ConnectionError:")):
            return redact_text(line[:300])
    return "评测引擎退出异常；请查看详细日志。"


def main() -> int:
    args = parse_args()
    batch_path = (ROOT / args.batch_file).resolve()
    batch = load_yaml(batch_path)
    strict_supplier = batch.get("test", {}).get("supplier_acceptance") is True
    if not strict_supplier:
        load_dotenv()
    providers_path = provider_config_path()
    providers = load_yaml(providers_path)
    commands = build_commands(batch, providers)

    run_id = new_run_id(str(batch.get("name", "batch")))
    run_dir = create_run_directory("chat", run_id)
    write_engine_handoff(args.result_file, run_dir)

    executable = shutil.which("evalscope")
    if executable is None and not args.dry_run:
        print("ERROR: evalscope command not found. Activate the EvalScope environment first.", file=sys.stderr)
        return 1

    manifest_commands = []
    failures = []

    for item in commands:
        task_output_dir = run_dir / f"{item['provider']}__{item['model']}"
        task_output_dir.mkdir(parents=True, exist_ok=True)

        supplier_config = prepare_supplier_config(batch["test"], item, task_output_dir) if strict_supplier else None
        command = [
            str(task_output_dir) if part == "__OUTPUT_DIR__" else str(supplier_config) if part == "__SUPPLIER_CONFIG__" else part
            for part in item["command_template"]
        ]

        manifest_commands.append(
            {
                "provider": item["provider"],
                "model": item["model"],
                "api_key_env": item["api_key_env"],
                "modality": item["modality"],
                "dataset": item["dataset"],
                "tokenizer_path": item["tokenizer_path"],
                "image_width": item["image_width"],
                "image_height": item["image_height"],
                "image_num": item["image_num"],
                "parallel": item["parallel"],
                "number": item["number"],
                "planned_requests_per_minute": item.get("planned_requests_per_minute"),
                "duration_seconds": item.get("duration_seconds"),
                "supplier_acceptance": strict_supplier,
                "supplier_workload": item.get("supplier_workload") if strict_supplier else None,
                "output_dir": str(task_output_dir.relative_to(ROOT)),
                "command": command_for_storage(command),
                "status": "dry_run" if args.dry_run else "pending",
            }
        )

    manifest = {
        "run_id": run_id,
        "batch_file": str(batch_path.relative_to(ROOT)),
        "batch_name": batch.get("name"),
        "description": batch.get("description"),
        "engine": batch.get("test", {}).get("engine", "evalscope_perf"),
        "command_count": len(manifest_commands),
        "provenance": {
            "created_at": datetime.now().astimezone().isoformat(),
            "git_revision": command_version(["git", "rev-parse", "HEAD"]),
            "python_version": platform.python_version(),
            "evalscope_version": command_version([executable, "--version"]) if executable else "not installed",
            "batch_config_sha256": sha256_file(batch_path),
            "providers_config_sha256": sha256_file(providers_path),
        },
        "commands": manifest_commands,
    }

    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nEvalScope 性能评测", flush=True)
    print(f"运行目录: {run_dir.relative_to(ROOT)}", flush=True)
    print(f"任务数: {len(commands)}", flush=True)

    for index, item in enumerate(manifest_commands, start=1):
        print(
            f"  [{index}/{len(manifest_commands)}] {item['provider']} / {item['model']}"
            f" · {display_value(item['number'])} 请求 · 并发 {display_value(item['parallel'])} · {item['dataset']}",
            flush=True,
        )

    if args.dry_run:
        print("预览完成：未调用模型。详细命令已写入 manifest.json。", flush=True)
        return 0

    env = os.environ.copy()
    for index, item in enumerate(manifest_commands, start=1):
        api_key_env = item.get("api_key_env")
        if api_key_env and not env.get(api_key_env):
            item["status"] = "skipped"
            item["reason"] = f"{api_key_env} is not set"
            failures.append({"provider": item["provider"], "model": item["model"], "reason": f"{api_key_env} is not set"})
            print(f"\nSKIP [{index}/{len(manifest_commands)}] {item['provider']} / {item['model']}: {api_key_env} is not set")
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            continue

        log_path = ROOT / item["output_dir"] / "benchmark.log"
        print(f"\n[{index}/{len(manifest_commands)}] {item['provider']} / {item['model']}", flush=True)
        print("  状态: 运行中", flush=True)
        started_at = time.monotonic()
        task_output_dir = ROOT / item["output_dir"]
        exit_code = (run_supplier_command(item["command"], env, api_key_env, task_output_dir, log_path)
                     if strict_supplier else run_command(item["command"], env, log_path))
        sanitize_run_artifacts(task_output_dir)
        item["status"] = "success" if exit_code == 0 else "failed"
        item["exit_code"] = exit_code
        elapsed = time.monotonic() - started_at
        status = "完成" if exit_code == 0 else f"失败（退出码 {exit_code}）"
        print(f"  状态: {status} · 耗时 {elapsed:.1f}s", flush=True)
        if exit_code != 0:
            print(f"  原因: {failure_reason(log_path)}", flush=True)
        if exit_code != 0:
            failures.append({"provider": item["provider"], "model": item["model"], "exit_code": exit_code})

        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    if failures:
        print(f"\nCompleted with {len(failures)} failure(s) or skipped task(s).")
        return 1

    print("\n所有 EvalScope 任务已完成。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
