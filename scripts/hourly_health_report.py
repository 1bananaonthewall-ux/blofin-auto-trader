#!/usr/bin/env python3
"""Read-only snapshot for Cursor hourly agent (no trades)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from leverage_intel import LeverageIntel, parse_instrument_max_leverage

TARGET = 50


def main() -> int:
    settings = load_settings()
    state_dir = settings.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    ex = BlofinExchange(settings)
    ex.load()
    intel = LeverageIntel(state_dir)
    intel.ingest_instruments(ex.list_instruments())

    equity = ex.fetch_equity_usdt()
    free = ex.fetch_free_equity_usdt()
    positions = ex.fetch_all_positions()

    rows = []
    bad = []
    for sym, pos in sorted(positions.items()):
        inst = int(pos.get("leverage") or 0)
        eff = int(pos.get("effective_leverage") or inst)
        cap = intel.resolve_target(sym, TARGET)
        ok = inst >= cap - 1 and eff >= cap - 1
        row = {
            "symbol": sym.split("/")[0],
            "inst": inst,
            "eff": eff,
            "cap": cap,
            "notional": round(float(pos.get("notional_usdt") or 0), 2),
            "ok": ok,
        }
        rows.append(row)
        if not ok:
            bad.append(row)

    tuning = {}
    tp = state_dir / "scalp_tuning.json"
    if tp.is_file():
        try:
            tuning = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            pass

    report = {
        "ts": time.time(),
        "equity": round(equity, 4),
        "free_margin": round(free, 4),
        "open_count": len(positions),
        "target_leverage": TARGET,
        "positions": rows,
        "non_compliant": bad,
        "tuning": tuning,
        "dry_run": settings.dry_run,
    }
    out = state_dir / "hourly_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
