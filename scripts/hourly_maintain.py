#!/usr/bin/env python3
"""
Hourly Blofin maintenance — run locally (Cursor agent or PowerShell).

  python scripts/hourly_maintain.py
  python scripts/hourly_maintain.py --no-close
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from leverage_intel import LeverageIntel, leverage_needs_reentry
from position_registry import PositionRegistry
from scalp_optimizer import ScalpOptimizer

MISSION_LEV = 50


def _log_line(state_dir: Path, record: dict) -> None:
    path = state_dir / "hourly_agent_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def close_non_compliant(
    ex: BlofinExchange,
    settings,
    registry: PositionRegistry,
    *,
    dry_run: bool,
) -> list[str]:
    closed: list[str] = []
    positions = ex.fetch_all_positions()
    for sym, pos in list(positions.items()):
        cap = ex.symbol_leverage_cap(sym)
        exch_max = ex.leverage_intel.exchange_max(sym) or cap
        inst = int(pos.get("leverage") or 0)
        eff = int(pos.get("effective_leverage") or inst)
        needs, reason = leverage_needs_reentry(
            pos, target_lev=MISSION_LEV, exchange_max=exch_max
        )
        if not needs and inst >= cap - 1 and eff >= cap - 1:
            continue
        if not needs:
            continue
        if dry_run:
            closed.append(f"DRY {sym.split('/')[0]} ({reason})")
            continue
        try:
            ex.cancel_pending_tpsl(sym)
            ex.close_position(sym, pos, False)
            registry.remove(sym)
            closed.append(f"{sym.split('/')[0]} ({reason})")
            time.sleep(0.25)
        except Exception as exc:
            closed.append(f"FAIL {sym.split('/')[0]}: {exc}")
    return closed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-close", action="store_true", help="Report only, no closes")
    args = ap.parse_args()

    settings = load_settings()
    state_dir = settings.state_dir
    ex = BlofinExchange(settings)
    ex.load()
    registry = PositionRegistry(state_dir)

    import subprocess

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "hourly_health_report.py")],
        cwd=str(ROOT),
        check=False,
    )

    report_path = state_dir / "hourly_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    closed: list[str] = []
    if not args.no_close:
        closed = close_non_compliant(ex, settings, registry, dry_run=settings.dry_run)

    opt_note = "skip"
    if settings.optimizer_enabled:
        opt = ScalpOptimizer(state_dir, settings)
        rep = opt.maybe_optimize(ex.fetch_equity_usdt(), force=True)
        opt_note = rep.summary if rep else "no change"

    record = {
        "ts": time.time(),
        "equity": report.get("equity"),
        "open": report.get("open_count"),
        "closed": closed,
        "optimizer": opt_note,
        "dry_run": settings.dry_run,
    }
    _log_line(state_dir, record)

    stamp = state_dir / "last_cursor_hourly.txt"
    stamp.write_text(str(time.time()), encoding="utf-8")
    due = ROOT / ".cursor" / "HOURLY_DUE"
    if due.is_file():
        due.unlink()

    print("=== hourly maintain ===")
    print(f"equity=${report.get('equity')} open={report.get('open_count')}")
    if closed:
        print("closed:", ", ".join(closed))
    else:
        print("closed: (none)")
    print("optimizer:", opt_note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
