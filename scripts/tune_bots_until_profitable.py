#!/usr/bin/env python3
"""Auto-tune bot gates until all 4 bots hit min return on every asset."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bobs_bots.simulator import compare_bots
from bobs_bots.tune import apply_tune_step, save_overrides

BOT_IDS = ["god-bot", "god-bot-scalper-pro", "god-bot-3r-fast", "god-bot-ml-cortex"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--assets", type=int, default=12)
    p.add_argument("--min-return", type=float, default=8.0)
    p.add_argument("--max-rounds", type=int, default=10)
    p.add_argument("--reset", action="store_true", help="Clear tune overrides before starting")
    args = p.parse_args()

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    if args.reset:
        save_overrides({})

    for round_i in range(1, args.max_rounds + 1):
        report = compare_bots(
            bot_ids=BOT_IDS,
            starting_pot=1000.0,
            start_date=start,
            end_date=end,
            max_assets=args.assets,
        )
        all_ok = True
        print(f"\n=== Round {round_i} ===")
        for bid in BOT_IDS:
            row = report["bots"][bid]
            losers = [r for r in row["results"] if r.get("return_pct", 0) < args.min_return]
            print(f"{row['name']}: avg={row['avg_return_pct']:.1f}% losers={len(losers)}/{len(row['results'])}")
            if losers:
                all_ok = False
                for loser in losers:
                    print(f"  - {loser.get('base')}: {loser.get('return_pct')}%")
                apply_tune_step(bid)
        if all_ok:
            print(f"\nAll bots >= {args.min_return}% on all {args.assets} assets.")
            return 0

    print("\nTune cap reached — review state/storefront/bot_tune_overrides.json")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
