#!/usr/bin/env python3
"""Trim stale equity_ticks outliers so the account curve matches live balance."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curve_guard import fetch_live_equity, repair_equity_curve


def main() -> int:
    state = ROOT / "state"
    path = state / "equity_ticks.jsonl"
    if not path.is_file():
        print("no equity_ticks.jsonl")
        return 1

    anchor, src = fetch_live_equity(state)
    result = repair_equity_curve(state, live_equity=anchor if anchor > 0 else None)
    print(
        f"repaired equity curve: {result.get('before')} -> {result.get('after')} ticks "
        f"(anchor=${float(result.get('anchor') or 0):.2f} via {src}, "
        f"band min={result.get('band_min')} max={result.get('band_max')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
