"""Shared configuration loading and provider catalog helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv() -> None:
    if os.environ.get("WORKBENCH_SKIP_DOTENV") == "1":
        return
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def provider_config_path() -> Path:
    """Allow an isolated supplier run to supply its own non-secret catalog."""
    value = os.environ.get("WORKBENCH_PROVIDERS_FILE")
    return Path(value).resolve() if value else ROOT / "configs" / "providers.yaml"


def parse_scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value == "":
        return ""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        return value


def load_simple_yaml(path: Path) -> dict:
    """Parse the small YAML subset used by this repository."""
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip():
            continue
        indent = len(line_without_comment) - len(line_without_comment.lstrip(" "))
        line = line_without_comment.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"List item without list parent in {path}: {raw_line}")
            item = line[2:]
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:\s", item):
                key, raw_value = (part.strip() for part in item.split(":", 1))
                mapping = {key: parse_scalar(raw_value) if raw_value else {}}
                parent.append(mapping)
                stack.append((indent, mapping))
            else:
                parent.append(parse_scalar(item))
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported YAML line in {path}: {raw_line}")
        key, raw_value = (part.strip() for part in line.split(":", 1))
        value = parse_scalar(raw_value) if raw_value else ([] if key in {"concurrency", "number", "cases", "images"} or parent is root.get("targets") else {})
        if not isinstance(parent, dict):
            raise ValueError(f"Mapping item without mapping parent in {path}: {raw_line}")
        parent[key] = value
        if isinstance(value, (dict, list)):
            stack.append((indent, value))
    return root


def load_yaml(path: Path) -> dict:
    if yaml is not None:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
        if not isinstance(data, dict):
            raise ValueError(f"Expected YAML object in {path}")
        return data
    return load_simple_yaml(path)


def provider_catalog(data: dict) -> dict:
    providers = data.get("providers", data)
    if not isinstance(providers, dict):
        raise ValueError("configs/providers.yaml must contain a providers mapping")
    return providers


def provider_connection(provider: dict) -> dict:
    connection = provider.get("connection", provider)
    if not isinstance(connection, dict) or "base_url" not in connection:
        raise ValueError("Provider connection must define base_url")
    return connection
