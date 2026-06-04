#!/usr/bin/env python3
"""Train local cortex from live bot state (trade outcomes, playbooks)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from local_cortex import train


def main() -> int:
    settings = load_settings()
    summary = train(settings.state_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
