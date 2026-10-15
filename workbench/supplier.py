"""Supplier orchestration and packaging; all HTTP scheduling stays in EvalScope."""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import urllib.parse
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACK_VERSION = "2.0.0"
ENGINE_VERSION = "1.13.0"
KEY_ENV = "SUPPLIER_API_KEY"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_config(config, demo=False):
    allowed = {"schema_version", "supplier", "base_url", "model", "region", "reasoning_mode", "max_output_tokens", "extra_body", "thresholds"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("配置含未知字段；密钥只用隐藏输入或 SUPPLIER_API_KEY，不写入文件。")
    if config.get("schema_version") != 1:
        raise ValueError("配置 schema_version 必须为 1。")
    for field in ("supplier", "base_url", "model", "region", "reasoning_mode"):
        value = config.get(field)
        if not isinstance(value, str) or not value.strip() or "填写" in value or "YOUR-" in value:
            raise ValueError(f"请填写 {field}。")
    url = urllib.parse.urlsplit(config["base_url"])
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Base URL 不得含账号、密钥、查询参数或片段。")
    if demo:
        if url.scheme != "http" or url.hostname != "127.0.0.1":
            raise ValueError("模拟模式只能连接本机 127.0.0.1。")
    elif url.scheme != "https" or url.hostname.endswith(".example"):
        raise ValueError("真实供应商必须使用 HTTPS 基础地址，例如 https://api.example.com/v1。")
    if url.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("请填写 API 基础地址（通常以 /v1 结束），不要包含 /chat/completions。")
    cap = config.get("max_output_tokens", 4096)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 32768:
        raise ValueError("max_output_tokens 必须为 1–32768 的整数。")
    extras = config.get("extra_body", {})
    if not isinstance(extras, dict):
        raise ValueError("extra_body 必须为对象。")
    forbidden = {"api_key", "apikey", "authorization", "headers", "password", "secret", "access_token", "token", "stream", "stream_options", "model", "messages", "max_tokens", "max_completion_tokens", "n", "ignore_eos", "tools", "tool_choice"}
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in forbidden:
                    raise ValueError("extra_body 包含认证或受测试标准控制的字段。")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(extras)
    thresholds = config.get("thresholds", {})
    if not isinstance(thresholds, dict) or set(thresholds) - {"min_success_rate", "ttft_p95_seconds", "ttfot_p95_seconds"}:
        raise ValueError("thresholds 字段无效。")
    for name, value in thresholds.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or (name == "min_success_rate" and value > 1)):
            raise ValueError("阈值应为正数或 null；成功率使用 0–1 小数。")
    return {**config, "base_url": config["base_url"].rstrip("/"), "max_output_tokens": cap, "extra_body": extras, "thresholds": thresholds}


def phases(short_demo=False):
    if short_demo:
        return [{"name": "connectivity", "count": 1, "rpm": None, "window_seconds": None}, {"name": "latency", "count": 6, "rpm": None, "window_seconds": None}, {"name": "rpm200", "count": 200, "rpm": 200, "window_seconds": 60}]
    return [{"name": "connectivity", "count": 1, "rpm": None, "window_seconds": None}, {"name": "latency", "count": 30, "rpm": None, "window_seconds": None}, {"name": "rpm50", "count": 25, "rpm": 50, "window_seconds": 30}, {"name": "rpm100", "count": 50, "rpm": 100, "window_seconds": 30}, {"name": "rpm200", "count": 2000, "rpm": 200, "window_seconds": 600}]


def make_plan(config, demo=False, short_demo=False):
    stages = phases(short_demo)
    count = sum(p["count"] for p in stages)
    return {"schema_version": 1, "pack_version": PACK_VERSION, "engine": {"name": "evalscope", "version": ENGINE_VERSION}, "demo": demo, "target": config, "phases": stages, "planned_requests": count, "output_token_upper_bound": count * config["max_output_tokens"], "billing_note": "仅输出上限；另有输入、思考及失败计费，未提供单价，不能当作费用估算。", "retries": 0, "network": "direct; TLS verified; redirects disabled", "ttft_origin": "调用 HTTP post 前，包含连接过程；不能与排除建连的其他工具直接横比", "ttft_end": "首非空白正文或独立 reasoning/reasoning_content", "ttfot_end": "首非空白 content 正文", "rate_method": "EvalScope 官方 workload_trace 固定时间戳调度，发完后等待在途请求结束", "phase_runs": []}


def configure(path):
    if path.exists():
        print(f"使用已有配置：{path}")
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    print("首次填写一次接口信息；API Key 不保存到配置。")
    labels = [("supplier", "供应商名称"), ("base_url", "API 基础地址，例如 https://api.example.com/v1"), ("model", "GLM 5.3 精确模型 ID（按接口实际名称）"), ("region", "测试机器地区/运营商或云区域"), ("reasoning_mode", "思考模式（填写实际默认或约定模式）")]
    config = {"schema_version": 1, **{name: input(label + "：").strip() for name, label in labels}, "max_output_tokens": 4096, "extra_body": {}, "thresholds": {"min_success_rate": None, "ttft_p95_seconds": None, "ttfot_p95_seconds": None}}
    config = validate_config(config)
    write_json(path, config)
    print(f"非敏感配置已保存：{path}")
    return config


def source_hashes():
    paths = sorted([*ROOT.glob("workbench/**/*.py"), *ROOT.glob("scripts/*.py"), *ROOT.glob("requirements*.txt")])
    return {str(p.relative_to(ROOT)): sha256(p) for p in paths if p.is_file()}


def isolated_env(key, providers):
    keep = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({KEY_ENV: key, "WORKBENCH_SKIP_DOTENV": "1", "WORKBENCH_PROVIDERS_FILE": str(providers), "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false", "NO_PROXY": "*", "no_proxy": "*", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "PYTHONUNBUFFERED": "1"})
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


PROMPTS = ["请用中文为初次使用模型API的客户写5条接入说明，涵盖密钥、模型ID、流式调用、用量和限流处理，约250字。不要输出代码。", "请用中文解释首生成内容时间、首回答正文时间、每分钟请求数和并发数的区别，给出一个具体例子，约250字。", "请用中文写一份模型API故障排查清单，涵盖鉴权失败、模型不存在、额度不足、限流和流中断，约250字。"]


def prepare_inputs(out, config, stages):
    directory = out / "inputs"
    directory.mkdir()
    provider_path = directory / "providers.yaml"
    write_json(provider_path, {"supplier": {"type": "openai-compatible", "base_url": config["base_url"], "api_key_env": KEY_ENV}})
    batches = {}
    for stage in stages:
        name = stage["name"]
        dataset = directory / f"{name}.jsonl"
        with dataset.open("w", encoding="utf-8") as stream:
            for index in range(stage["count"]):
                body = {**config["extra_body"], "model": config["model"], "messages": [{"role": "user", "content": f"样本编号 {index:06d}。\n" + PROMPTS[index % len(PROMPTS)]}], "stream": True, "max_tokens": config["max_output_tokens"], "stream_options": {"include_usage": True}}
                stream.write(json.dumps(body, ensure_ascii=False) + "\n")
        test = {"engine": "evalscope_perf", "modality": "text", "supplier_acceptance": True, "dataset": "line_by_line", "dataset_path": str(dataset), "concurrency": [1], "number": [stage["count"]], "timeout_seconds": 60, "total_timeout_seconds": 60, "connect_timeout_seconds": 10, "max_tokens": config["max_output_tokens"], "stream": True, "test_connection": False, "warmup_num": 0}
        if stage["rpm"]:
            test.update(requests_per_minute=stage["rpm"], duration_seconds=stage["window_seconds"], open_loop=True)
        batch = {"schema_version": 1, "name": f"supplier-{name}", "targets": {"supplier": [config["model"]]}, "test": test}
        path = directory / f"{name}.yaml"
        write_json(path, batch)  # JSON is a strict subset of YAML; no additional parser.
        batches[name] = path
    return provider_path, batches


def redact_files(out, key):
    if not key:
        return
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".yaml", ".yml", ".csv", ".html", ".log", ".txt"}:
            value = path.read_text(encoding="utf-8", errors="replace")
            if key in value:
                path.write_text(value.replace(key, "[REDACTED]"), encoding="utf-8")


def archive_results(out, secret=None):
    # Check binary evidence too (including the native SQLite database).
    # Refuse export if upstream echoed a credential that escaped redaction.
    if secret:
        encoded = secret.encode("utf-8")
        if any(encoded in p.read_bytes() for p in out.rglob("*") if p.is_file()):
            raise ValueError("结果中仍检测到认证密钥，已阻止生成回传 ZIP。请先修复脱敏后重新打包。")
    checksums = out / "SHA256SUMS.txt"
    checksums.write_text("".join(f"{sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in sorted(out.rglob("*")) if p.is_file() and p != checksums), encoding="utf-8")
    archive = out.parent / (out.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                bundle.write(path, (Path(out.name) / path.relative_to(out)).as_posix())
    return archive


def run_child(command, env, log, timeout):
    with log.open("w", encoding="utf-8") as stream:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        try:
            return proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()
            return 124


def execute(config, demo=False, short_demo=False, server=None):
    if importlib.metadata.version("evalscope") != ENGINE_VERSION:
        raise ValueError(f"版本不匹配，请重新运行安装脚本；要求 EvalScope {ENGINE_VERSION}。")
    key = "LOCAL_MOCK_NOT_A_SECRET" if demo else os.environ.get(KEY_ENV) or getpass.getpass("API Key（隐藏输入，不保存）：")
    if not key:
        raise ValueError("未提供 API Key。")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = ROOT / "results" / "supplier" / (("mock-" if demo else "supplier-") + stamp)
    out.mkdir(parents=True, exist_ok=False)
    manifest = make_plan(config, demo, short_demo)
    manifest.update(started_at=dt.datetime.now(dt.timezone.utc).isoformat(), source_sha256=source_hashes(), runtime={"python": sys.version.split()[0], "platform": sys.platform})
    write_json(out / "manifest.json", manifest)
    write_json(out / "environment.json", {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")})
    providers, batches = prepare_inputs(out, config, manifest["phases"])
    env = isolated_env(key, providers)
    from workbench.reports.supplier import build_report, summarize_phase
    try:
        for phase in manifest["phases"]:
            name = phase["name"]
            print(f"开始 {name}：{phase['count']} 次；{str(phase['rpm']) + ' RPM' if phase['rpm'] else '逐条请求'}", flush=True)
            batch = str(batches[name].relative_to(ROOT))
            for mode, command in [("preflight", [sys.executable, "scripts/check_env.py", batch, "--json"]), ("plan", [sys.executable, "scripts/run_benchmark.py", batch, "--dry-run"])]:
                rc = run_child(command, env, out / f"{name}-{mode}.log", 120)
                if rc != 0:
                    raise ValueError(f"{name} {mode} 未完成，详见结果包中的日志。")
            handoff = out / f"{name}-handoff.json"
            timeout = phase["window_seconds"] + 180 if phase["rpm"] else phase["count"] * 65 + 120
            rc = run_child([sys.executable, "scripts/run_benchmark.py", batch, "--html", "--result-file", str(handoff)], env, out / f"{name}-execution.log", timeout)
            destination = out / name
            if handoff.exists():
                result = json.loads(handoff.read_text(encoding="utf-8"))
                raw = Path(result.get("run_dir") or "")
                if raw.is_dir() and raw.resolve().is_relative_to((ROOT / "results" / "runs").resolve()):
                    redact_files(raw, key)
                    shutil.copytree(raw, destination)
            manifest["phase_runs"].append({**phase, "status": "completed" if rc == 0 else "failed", "exit_code": rc, "run_dir": name})
            write_json(out / "manifest.json", manifest)
            result = summarize_phase(destination, phase)
            if rc != 0 or result.get("valid_success", 0) == 0 or result.get("evidence_status") != "complete":
                print("本阶段未跑通或证据不完整，停止后续加压并保留报告。", flush=True)
                break
    except (ValueError, OSError, json.JSONDecodeError) as error:
        manifest["orchestration_error"] = str(error).replace(key, "[REDACTED]")
        print(manifest["orchestration_error"], file=sys.stderr)
    finally:
        manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json(out / "manifest.json", manifest)
        if server is not None:
            write_json(out / "mock-server-arrivals.json", server.records_snapshot())
        redact_files(out, key)
        summary = build_report(out)
        archive = archive_results(out, key)
        print(f"\n报告：{out / 'report.html'}\n回传这一个 ZIP：{archive}", flush=True)
    return out, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="GLM 5.3 供应商自测：统一预检、执行与报告回传")
    parser.add_argument("command", nargs="?", choices=["setup", "plan", "run", "demo"], default="setup")
    parser.add_argument("--config", type=Path, default=ROOT / "supplier.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--full", action="store_true", help="本地模拟也执行正式 2106 次/200 RPM 十分钟计划")
    options = parser.parse_args(argv)
    server = None
    try:
        if options.command == "demo":
            from workbench.testing.supplier_mock import start_mock_server
            server, _ = start_mock_server()
            config = {"schema_version": 1, "supplier": "本机模拟供应商（非真实GLM）", "base_url": server.base_url, "model": "mock-glm-5.3", "region": "127.0.0.1 本机", "reasoning_mode": "模拟分离思考与正文", "max_output_tokens": 4096, "extra_body": {}, "thresholds": {"min_success_rate": None, "ttft_p95_seconds": None, "ttfot_p95_seconds": None}}
        elif options.command == "setup":
            config = configure(options.config)
        else:
            config = json.loads(options.config.read_text(encoding="utf-8"))
        demo = options.command == "demo"
        config = validate_config(config, demo)
        plan = make_plan(config, demo, demo and not options.full)
        print(f"\n{'本机模拟' if demo else '真实接口'}测试计划：{plan['planned_requests']} 次请求；最后 200 RPM 持续 {plan['phases'][-1]['window_seconds']} 秒。")
        print(f"输出 Token 上限 {plan['output_token_upper_bound']:,}，另计输入/思考；不是预计费用。")
        print("首生成内容/首正文及成功率阈值未配置时只测量，不宣称 SLA 通过。")
        if options.command == "plan" or (options.command == "run" and not options.execute):
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            print("未调用模型。确认测试窗口与额度后加 --execute。")
            return 0
        if options.command == "setup" and input("确认时间窗口和额度后输入 RUN 开始；其他输入只保存配置：").strip() != "RUN":
            print("配置已保存，未调用模型。")
            return 0
        _, summary = execute(config, demo, demo and not options.full, server)
        return 0 if summary.get("complete") and summary.get("assessment") not in {"incomplete", "not_passed"} else 1
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as error:
        print(f"未执行或已停止：{error}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
