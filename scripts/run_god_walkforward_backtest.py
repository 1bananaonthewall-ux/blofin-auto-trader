#!/usr/bin/env python3
"""Run God Bot walk-forward backtest on full Blofin universe."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    p = argparse.ArgumentParser(description="God Bot walk-forward universe backtest")
    p.add_argument("--starting-pot", type=float, default=1000.0)
    p.add_argument("--lookback-days", type=int, default=3650, help="Request up to ~10y (API depth varies)")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-assets", type=int, default=0, help="0 = full universe")
    p.add_argument("--train-days", type=int, default=120)
    p.add_argument("--test-days", type=int, default=30)
    p.add_argument("--step-days", type=int, default=None)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--no-ws", action="store_true", help="Disable websocket tail sync")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--refit-ml", action="store_true", help="Forward ML refit each train fold (in-memory only; never overwrites live model)")
    p.add_argument("--apply-live", action="store_true", help="Push walk-forward winners to live god-bot gates (default: report only)")
    p.add_argument("--smoke", action="store_true", help="Quick test: 5 symbols, 60d lookback")
    args = p.parse_args()

    if args.smoke:
        args.max_assets = 5
        args.lookback_days = 60
        args.train_days = 21
        args.test_days = 7

    from god_backtest.engine import run_walkforward_backtest

    report = run_walkforward_backtest(
        starting_pot=args.starting_pot,
        lookback_days=args.lookback_days,
        start_date=args.start_date,
        end_date=args.end_date,
        max_assets=args.max_assets,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        max_workers=args.workers,
        use_ws_tail=not args.no_ws,
        refit_ml=args.refit_ml,
        use_cache=not args.no_cache,
        apply_live=args.apply_live,
    )
    print(json.dumps(
        {
            "ok": report.get("ok"),
            "avg_oos_score": report.get("avg_oos_score"),
            "folds": report.get("folds"),
            "assets_tested": report.get("assets_tested"),
            "recommended_params": report.get("recommended_params"),
            "elapsed_sec": report.get("elapsed_sec"),
            "report": str(ROOT / "state" / "god_backtest" / "walkforward_report.json"),
        },
        indent=2,
    ))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
