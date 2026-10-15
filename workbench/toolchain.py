"""Resolve pinned tools without installing or mutating the global runtime."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from workbench.config import ROOT


PROMPTFOO_VERSION = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["devDependencies"]["promptfoo"]


def promptfoo_executable(root: Path = ROOT) -> str | None:
    """Prefer npm ci's local executable; retain the pinned global CLI fallback."""
    local = root / "node_modules" / ".bin" / "promptfoo"
    return str(local) if local.is_file() else shutil.which("promptfoo")


def promptfoo_version(executable: str | None = None) -> str | None:
    executable = executable or promptfoo_executable()
    if not executable:
        return None
    try:
        output = subprocess.run(
            [executable, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if output.returncode:
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", output.stdout)
    return match.group(1) if match else None
