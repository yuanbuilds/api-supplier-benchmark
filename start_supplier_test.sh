#!/usr/bin/env bash
# Install only the pinned Python tools needed for the supplier acceptance path.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
  candidate=""
  for bin in python3.12 python3.11 python3.10 python3; do
    if command -v "$bin" >/dev/null 2>&1 && "$bin" -c 'import sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,12)))' 2>/dev/null; then
      candidate="$bin"
      break
    fi
  done
  if [[ -z "$candidate" ]]; then
    echo '需要 Python 3.10–3.12（推荐 3.12）。请安装后重新运行；Windows 请在 WSL 中运行。' >&2
    exit 2
  fi
  "$candidate" -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,12)))'; then
  echo '现有 .venv 不是 Python 3.10–3.12，请另建工作目录并使用支持的 Python。' >&2
  exit 2
fi

fingerprint="$(.venv/bin/python -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("requirements.txt").read_bytes()).hexdigest())')"
if [[ ! -f .venv/.supplier-requirements.sha256 ]] || [[ "$(cat .venv/.supplier-requirements.sha256)" != "$fingerprint" ]] || ! .venv/bin/python -c 'from importlib.metadata import version; assert version("evalscope")=="1.13.0"; import aiohttp, yaml, PIL' >/dev/null 2>&1; then
  echo '首次安装锁定版本的 Python 依赖（可能需要数分钟）；不会安装 Node 或 Promptfoo。'
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m pip check
  printf '%s\n' "$fingerprint" > .venv/.supplier-requirements.sha256
fi
exec .venv/bin/python scripts/supplier_test.py "$@"
