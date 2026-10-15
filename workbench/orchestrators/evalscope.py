#!/usr/bin/env python3
"""Run the EvalScope engine; the public runner owns post-run finalization."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an EvalScope batch.",
    )
    parser.add_argument("batch_file", help="Batch config, for example evals/performance/evalscope/chat.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview commands and reports only. EvalScope will not run.",
    )
    parser.add_argument("--result-file", help="Write the engine-created run directory for the public runner.")
    return parser.parse_args()


def run_step(label: str, command: list[str], allow_failure: bool = False, stream_output: bool = False, announce: bool = True) -> tuple[int, str]:
    if announce:
        print(f"\n{label}…", flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output_parts = []
    assert process.stdout is not None
    if stream_output:
        for line in process.stdout:
            output_parts.append(line)
            print(line, end="", flush=True)
    else:
        output_parts.append(process.stdout.read())
    process.wait()
    output = "".join(output_parts)
    if process.returncode != 0 and not allow_failure:
        print(f"{label}失败（退出码 {process.returncode}）。请查看运行目录中的报告和日志。", flush=True)
        raise SystemExit(process.returncode)
    if process.returncode == 0 and not stream_output and announce:
        print(f"{label}完成。", flush=True)
    return process.returncode, output


def should_generate_reports(batch_exit_code: int, dry_run: bool = False) -> bool:
    """Only completed provider runs may produce a performance conclusion."""
    return batch_exit_code == 0 and not dry_run


def main() -> int:
    args = parse_args()

    run_batch_command = [sys.executable, "-m", "workbench.engines.evalscope", args.batch_file]
    if args.result_file:
        run_batch_command.extend(["--result-file", args.result_file])
    if args.dry_run:
        run_batch_command.append("--dry-run")

    batch_exit_code, batch_output = run_step("执行 EvalScope", run_batch_command, allow_failure=True, stream_output=True)
    if batch_exit_code != 0:
        print("\n评测执行失败：已跳过性能汇总、准入报告和统一摘要。请先处理供应商错误后重新运行。", flush=True)
        return batch_exit_code
    if args.dry_run:
        print("\n预览完成：未生成性能汇总、准入报告或统一摘要。", flush=True)
    return batch_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
