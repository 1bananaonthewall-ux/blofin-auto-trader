#!/usr/bin/env python3
"""Verify all 4 bots are profitable on every asset in universe sample."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bobs_bots.simulator import compare_bots

BOT_IDS = ["god-bot", "god-bot-scalper-pro", "god-bot-3r-fast", "god-bot-ml-cortex"]
MIN_RETURN_PCT = 5.0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--assets", type=int, default=15)
    p.add_argument("--min-return", type=float, default=MIN_RETURN_PCT)
    p.add_argument("--out", type=str, default="state/storefront/bot_verify.json")
    args = p.parse_args()

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    report = compare_bots(
        bot_ids=BOT_IDS,
        starting_pot=1000.0,
        start_date=start,
        end_date=end,
        max_assets=args.assets,
    )

    ok = True
    lines: list[str] = []
    for bid in BOT_IDS:
        row = report["bots"].get(bid, {})
        results = row.get("results", [])
        losers = [r for r in results if r.get("return_pct", 0) < args.min_return]
        weak = [r for r in results if r.get("return_pct", 0) < args.min_return]
        lines.append(
            f"{row.get('name', bid)}: avg={row.get('avg_return_pct', 0):.1f}% "
            f"assets={len(results)} below_min={len(weak)}"
        )
        if weak:
            ok = False
            for w in weak[:5]:
                lines.append(f"  - {w.get('base')}: {w.get('return_pct')}% ({w.get('trades')} trades)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Period {start} -> {end} | min return {args.min_return}% per asset")
    print("\n".join(lines))
    print(f"\nReport: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
