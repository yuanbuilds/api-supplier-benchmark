"""Isolated EvalScope entry point. Its argv/config never contains an API key."""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != Path(__file__).resolve().parent]
sys.path.insert(0, str(ROOT))


def main():
    if importlib.metadata.version("evalscope") != "1.13.0":
        raise RuntimeError("Supplier acceptance requires evalscope==1.13.0")
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from pydantic import SecretStr
    from evalscope.perf.arguments import Arguments
    from evalscope.perf.main import run_perf_benchmark
    from workbench.engines import supplier_api  # noqa: F401; public register_api extension

    key = os.environ.pop("WORKBENCH_SUPPLIER_KEY", "")
    kwargs = config["arguments"]
    if key:
        kwargs["api_key"] = SecretStr(key)
    os.environ["WORKBENCH_SUPPLIER_RECORDS"] = config["records_file"]
    os.environ["WORKBENCH_SUPPLIER_SCHEDULE"] = config["schedule_file"]
    os.environ["WORKBENCH_SUPPLIER_RPM"] = str(config.get("requests_per_minute") or 0)
    run_perf_benchmark(Arguments(**kwargs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
