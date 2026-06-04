#!/usr/bin/env python3
"""Re-attach all open positions with current fee-aware TPSL policy (fixes wide liq-gap brackets)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from position_registry import PositionRegistry


def main() -> int:
    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    reg = PositionRegistry(settings.state_dir)
    n = ex.repair_all_open_tpsl(settings, registry=reg)
    print(f"repaired {n} position(s) with policy TPSL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
