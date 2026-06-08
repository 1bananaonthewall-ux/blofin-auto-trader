#!/usr/bin/env python3
"""Compare God Bot vs Bob's three bots on real confluence backtests."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bobs_bots.simulator import compare_bots


def main() -> int:
    p = argparse.ArgumentParser(description="Compare God Bot vs Bob's Bots backtests")
    p.add_argument("--days", type=int, default=120, help="Lookback days if dates omitted")
    p.add_argument("--start", type=str, default="", help="Start YYYY-MM-DD")
    p.add_argument("--end", type=str, default="", help="End YYYY-MM-DD")
    p.add_argument("--pot", type=float, default=1000.0)
    p.add_argument("--assets", type=int, default=8, help="Top N by volume")
    p.add_argument("--symbols", type=str, default="", help="Comma inst_ids e.g. BTC-USDT,ETH-USDT")
    p.add_argument("--out", type=str, default="", help="Write JSON report path")
    args = p.parse_args()

    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = args.start or (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    inst = [s.strip() for s in args.symbols.split(",") if s.strip()] or None

    report = compare_bots(
        bot_ids=["god-bot", "god-bot-scalper-pro", "god-bot-3r-fast", "god-bot-ml-cortex"],
        starting_pot=args.pot,
        start_date=start,
        end_date=end,
        inst_ids=inst,
        max_assets=args.assets,
    )

    print(f"Period: {report['period']['start_date']} -> {report['period']['end_date']}")
    print(f"Starting pot: ${report['starting_pot']}")
    print()
    ranked = sorted(
        report["bots"].items(),
        key=lambda kv: kv[1].get("avg_return_pct", 0),
        reverse=True,
    )
    print(f"{'Bot':<28} {'AvgRet%':>8} {'AvgPF':>7} {'AvgWR%':>7} {'Trades':>7} {'OK':>4}")
    print("-" * 64)
    for bid, row in ranked:
        print(
            f"{row['name']:<28} {row['avg_return_pct']:>8.2f} {row['avg_profit_factor']:>7.2f} "
            f"{row['avg_win_rate_pct']:>7.1f} {row['total_trades']:>7} {row['assets_ok']:>4}"
        )

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
