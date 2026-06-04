#!/usr/bin/env python3
"""One-screen mission + stack health (mission_config sole objective)."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from growth_optimizer import CompoundGrowthOptimizer
from mission_config import TARGET_DAILY_GROWTH_PCT, progress_toward_daily_goal_pct, sole_objective_label
from growth_optimizer import _day_start_equity


def main() -> int:
    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    eq = ex.fetch_equity_usdt()
    fm = ex.fetch_free_equity_usdt()
    pos = ex.fetch_all_positions()

    growth = CompoundGrowthOptimizer(settings.state_dir)
    m = growth.get_growth_metrics(eq)
    day_start = _day_start_equity(growth.history, eq)
    today_pct = (eq / day_start - 1.0) * 100.0 if day_start > 0 and eq > 0 else 0.0
    progress = progress_toward_daily_goal_pct(today_pct)

    lines = [
        f"MISSION {sole_objective_label()}",
        f"equity=${eq:.4f} free=${fm:.4f} today={today_pct:+.2f}% ({progress:.1f}% of +{TARGET_DAILY_GROWTH_PCT:.0f}% goal)",
        f"need +{TARGET_DAILY_GROWTH_PCT:.0f}% today | EOD=${m.projected_capital_at_target:,.4f} | on_track={m.on_track}",
        f"open={len(pos)} | live={not settings.dry_run} mode={settings.mode}",
    ]
    for sym, p in sorted(pos.items()):
        inst = int(p.get("leverage") or 0)
        eff = int(p.get("effective_leverage") or inst)
        cap = ex.symbol_leverage_cap(sym)
        lines.append(
            f"  {sym.split('/')[0]:6s} {p.get('side','?')} inst={inst:3d}x eff={eff:3d}x cap={cap}x"
        )

    hr = settings.state_dir / "hourly_report.json"
    if hr.is_file():
        try:
            h = json.loads(hr.read_text(encoding="utf-8"))
            t = h.get("tuning", {})
            lines.append(
                f"tph={t.get('trades_last_hour', '?')} wins/hr={t.get('wins_last_hour', '?')} "
                f"optimizer={t.get('action', '?')}"
            )
        except Exception:
            pass

    out = settings.state_dir / "stack_status.txt"
    text = "\n".join(lines)
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
