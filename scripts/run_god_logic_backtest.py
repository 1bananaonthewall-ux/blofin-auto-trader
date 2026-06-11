#!/usr/bin/env python3
"""
Backtest God Bot confluence-core logic on the full tradeable universe.

Optimizes for balanced win-rate + return (not gates-only), then applies to supercharge.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "state" / "god_logic_backtest.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("god_logic_backtest")


def _universe(max_assets: int) -> list[str]:
    from storefront_market import list_tradeable_assets

    assets = list_tradeable_assets()
    if max_assets > 0:
        assets = assets[:max_assets]
    return [a["inst_id"] for a in assets]


def main() -> int:
    p = argparse.ArgumentParser(description="God Bot confluence logic backtest (full universe)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--start", type=str, default="")
    p.add_argument("--end", type=str, default="")
    p.add_argument("--pot", type=float, default=1000.0)
    p.add_argument("--assets", type=int, default=0, help="0 = all tradeable symbols")
    p.add_argument("--holdout-pct", type=float, default=0.30)
    p.add_argument("--apply-supercharge", action="store_true")
    p.add_argument("--no-optimize", action="store_true")
    p.add_argument("--workers", type=int, default=4, help="Parallel candle load workers (keep low to avoid 429)")
    p.add_argument("--no-ws", action="store_true", help="Skip WebSocket tail sync")
    args = p.parse_args()

    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = args.start or (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    span = end_ms - start_ms
    train_end_ms = int(start_ms + span * (1.0 - args.holdout_pct))
    test_start_ms = train_end_ms

    inst_ids = _universe(args.assets)
    log.info(
        "God logic backtest | %s -> %s | %d symbols | train->%s holdout %.0f%%",
        start,
        end,
        len(inst_ids),
        datetime.fromtimestamp(train_end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        args.holdout_pct * 100,
    )

    report: dict = {
        "logic": "confluence_core",
        "period": {"start_date": start, "end_date": end, "start_ms": start_ms, "end_ms": end_ms},
        "holdout_pct": args.holdout_pct,
        "train_end_date": datetime.fromtimestamp(train_end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "starting_pot": args.pot,
        "symbol_count": len(inst_ids),
        "symbols_sample": inst_ids[:12],
    }

    from god_backtest.growth_agent_bt import (
        GROWTH_LOGIC_GRID,
        GrowthBtConfig,
        backtest_growth_portfolio,
        build_growth_universe,
        optimize_growth_params,
        portfolio_score_balanced,
        spec_from_growth_params,
        validate_holdout,
    )
    from god_backtest.trade_safe import recommend_workers, set_below_normal_priority

    set_below_normal_priority()
    use_ws = not args.no_ws
    workers = recommend_workers(max(1, args.workers), live_safe=True)
    universe = build_growth_universe(
        inst_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        use_cache=True,
        use_ws_tail=use_ws,
        max_workers=workers,
    )
    report["prefetch"] = {
        "symbols_loaded": len(universe.symbols),
        "symbols_requested": len(inst_ids),
        "use_ws_tail": use_ws,
        "workers": workers,
    }

    if args.no_optimize:
        pol_path = ROOT / "state" / "growth_supercharge.json"
        if pol_path.is_file():
            pol = json.loads(pol_path.read_text(encoding="utf-8"))
            spec = spec_from_growth_params(
                min_confluence=pol.get("min_confluence"),
                min_composite_score=pol.get("min_signal_score"),
                min_confidence=pol.get("min_confidence"),
                min_agreeing=pol.get("min_agreeing", 4),
                entry_gap_bars=pol.get("entry_gap_bars", 5),
                risk_per_trade=pol.get("margin_pct_per_trade", 2.2) / 100.0,
                skip_choppy=pol.get("skip_choppy", True),
            )
            scan_bars = int(pol.get("scan_every_bars", 6))
        else:
            spec = spec_from_growth_params(
                min_confluence=0.47,
                min_composite_score=47.0,
                min_confidence=0.55,
                min_agreeing=4,
                entry_gap_bars=5,
                risk_per_trade=0.022,
                skip_choppy=True,
            )
            scan_bars = 6
        best_params = {}
        best_result = backtest_growth_portfolio(
            GrowthBtConfig(spec=spec, scan_every_bars=scan_bars),
            inst_ids,
            starting_pot=args.pot,
            start_ms=start_ms,
            end_ms=end_ms,
            universe=universe,
        )
        holdout_row = None
        report["full_period"] = best_result
    else:
        opt = optimize_growth_params(
            inst_ids,
            starting_pot=args.pot,
            start_ms=start_ms,
            end_ms=train_end_ms,
            load_end_ms=end_ms,
            grid=GROWTH_LOGIC_GRID,
            score_fn=portfolio_score_balanced,
            universe=universe,
            use_ws_tail=False,
            max_workers=workers,
        )
        report["optimization"] = opt
        best_params = opt["best_params"]
        best_result = opt["best_result"]

        holdout_row = validate_holdout(
            best_params,
            inst_ids,
            starting_pot=args.pot,
            train_end_ms=train_end_ms,
            test_start_ms=test_start_ms,
            test_end_ms=end_ms,
            universe=universe,
        )
        report["holdout"] = holdout_row

        full_row = backtest_growth_portfolio(
            GrowthBtConfig(
                spec=spec_from_growth_params(**{k: v for k, v in best_params.items() if k != "scan_every_bars"}),
                scan_every_bars=int(best_params.get("scan_every_bars", 6)),
            ),
            inst_ids,
            starting_pot=args.pot,
            start_ms=start_ms,
            end_ms=end_ms,
            universe=universe,
        )
        report["full_period"] = full_row

        legacy = backtest_growth_portfolio(
            GrowthBtConfig(spec=spec_from_growth_params("god-bot")),
            inst_ids,
            starting_pot=args.pot,
            start_ms=start_ms,
            end_ms=end_ms,
            universe=universe,
        )
        report["baseline_god_bot"] = legacy

        log.info(
            "TRAIN ret=%.2f%% PF=%.2f WR=%.1f%% DD=%.2f%%",
            best_result.get("return_pct", 0),
            best_result.get("profit_factor", 0),
            best_result.get("win_rate_pct", 0),
            best_result.get("max_drawdown_pct", 0),
        )
        log.info(
            "HOLDOUT ret=%.2f%% PF=%.2f WR=%.1f%% trades=%d",
            holdout_row.get("return_pct", 0),
            holdout_row.get("profit_factor", 0),
            holdout_row.get("win_rate_pct", 0),
            holdout_row.get("trades", 0),
        )
        log.info("BEST params: %s", json.dumps(best_params))

    report["best_params"] = best_params
    report["best_result"] = best_result
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("Wrote %s", REPORT_PATH)

    print()
    print(f"Period: {start} -> {end}  |  pot=${args.pot:.0f}  |  symbols={len(inst_ids)}")
    print()
    for label, row in [
        ("TRAIN (optimized)", best_result),
        ("HOLDOUT (OOS)", report.get("holdout")),
        ("FULL period", report.get("full_period")),
        ("god-bot base", report.get("baseline_god_bot")),
    ]:
        if not row or row.get("error"):
            continue
        print(
            f"{label:22} ret={row.get('return_pct', 0):>7.2f}%  "
            f"PF={row.get('profit_factor', 0):>5.2f}  "
            f"DD={row.get('max_drawdown_pct', 0):>6.2f}%  "
            f"trades={row.get('trades', 0):>4}  "
            f"WR={row.get('win_rate_pct', 0):>5.1f}%"
        )

    if args.apply_supercharge and best_params:
        from growth_supercharge import apply_from_backtest_report

        apply_from_backtest_report(report, ROOT / "state")
        print("\nApplied confluence-core logic + tuning -> state/growth_supercharge.json")
        print("Restart God Bot to load new logic.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
