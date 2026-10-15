#!/usr/bin/env python3
"""Stable supplier-facing entry point for the existing benchmark workbench."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.supplier import main

if __name__ == "__main__":
    raise SystemExit(main())
