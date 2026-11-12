#!/usr/bin/env python3
"""Launcher for the LangWars stress pack."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langwars_stress.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
