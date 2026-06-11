#!/usr/bin/env python3
"""
A/B backtest: God Bot logic with vs without cortex copilot (Qwen learning path).

Uses walk-forward cortex memory on historical closes — no lookahead.
Default: BTC-USDT, max available history from Blofin cache/API.
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

REPORT_PATH = ROOT / "state" / "copilot_ab_backtest.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copilot_ab")


def main() -> int:
    p = argparse.ArgumentParser(description="Copilot A/B backtest (with vs without Qwen learning path)")
    p.add_argument("--symbol", default="BTC-USDT", help="Blofin inst_id e.g. BTC-USDT")
    p.add_argument("--days", type=int, default=0, help="0 = request max (~10y)")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--pot", type=float, default=1000.0)
    p.add_argument("--no-ws", action="store_true")
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args()

    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.start:
        start = args.start
    else:
        lookback = args.days if args.days > 0 else 3650
        start = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%d")

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    from god_backtest.copilot_memory import CortexMemory
    from god_backtest.growth_agent_bt import (
        GROWTH_LOGIC_GRID,
        GrowthBtConfig,
        backtest_growth_portfolio,
        build_growth_universe,
        portfolio_score_balanced,
        spec_from_growth_params,
    )
    from god_backtest.trade_safe import recommend_workers, set_below_normal_priority

    set_below_normal_priority()
    inst_ids = [args.symbol]
    workers = recommend_workers(max(1, args.workers), live_safe=True)

    log.info("Loading %s candles %s -> %s", args.symbol, start, end)
    universe = build_growth_universe(
        inst_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        use_cache=True,
        use_ws_tail=not args.no_ws,
        max_workers=workers,
    )
    if not universe.symbols:
        log.error("No candle data for %s in range", args.symbol)
        return 1

    bars = len(universe.c5[universe.symbols[0]])
    first_ts = universe.c5[universe.symbols[0]][0][0]
    last_ts = universe.c5[universe.symbols[0]][-1][0]
    actual_start = datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    actual_end = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    log.info("Data: %d 5m bars | %s -> %s", bars, actual_start, actual_end)

    # Pick balanced params on train window (first 70%), test on full walk for A/B fairness
    train_end_ms = int(start_ms + (end_ms - start_ms) * 0.70)
    best_score = -1e18
    best_params = GROWTH_LOGIC_GRID[0]
    for params in GROWTH_LOGIC_GRID:
        raw = dict(params)
        scan_bars = int(raw.pop("scan_every_bars", 8))
        spec = spec_from_growth_params(**raw)
        cfg = GrowthBtConfig(spec=spec, max_positions=1, scan_every_bars=scan_bars)
        row = backtest_growth_portfolio(
            cfg,
            inst_ids,
            starting_pot=args.pot,
            start_ms=start_ms,
            end_ms=train_end_ms,
            universe=universe,
            use_copilot=False,
        )
        sc = portfolio_score_balanced(row)
        if sc > best_score:
            best_score = sc
            best_params = params

    raw = dict(best_params)
    scan_bars = int(raw.pop("scan_every_bars", 8))
    spec = spec_from_growth_params(**raw)
    cfg = GrowthBtConfig(spec=spec, max_positions=1, scan_every_bars=scan_bars)

    log.info("Running baseline (no copilot) on full range...")
    baseline = backtest_growth_portfolio(
        cfg,
        inst_ids,
        starting_pot=args.pot,
        start_ms=start_ms,
        end_ms=end_ms,
        universe=universe,
        use_copilot=False,
    )

    log.info("Running cortex copilot (walk-forward learning) on full range...")
    copilot_mem = CortexMemory()
    with_copilot = backtest_growth_portfolio(
        cfg,
        inst_ids,
        starting_pot=args.pot,
        start_ms=start_ms,
        end_ms=end_ms,
        universe=universe,
        use_copilot=True,
        copilot_strict=True,
        copilot_memory=copilot_mem,
    )

    b_score = portfolio_score_balanced(baseline)
    c_score = portfolio_score_balanced(with_copilot)
    winner = "copilot" if c_score > b_score else "baseline"
    if abs(c_score - b_score) < 0.5:
        winner = "tie"

    report = {
        "symbol": args.symbol,
        "requested": {"start": start, "end": end},
        "actual_bars": {"start": actual_start, "end": actual_end, "bars_5m": bars},
        "starting_pot": args.pot,
        "params": best_params,
        "baseline": baseline,
        "with_copilot": with_copilot,
        "scores": {
            "baseline_balanced": round(b_score, 2),
            "copilot_balanced": round(c_score, 2),
            "winner": winner,
        },
        "hypothesis": {
            "copilot_late_catches_up": None,
            "note": "Compare early_half vs late_half win rate / pnl below",
        },
        "learning_curve": {
            "baseline": {
                "early_half": baseline.get("early_half"),
                "late_half": baseline.get("late_half"),
            },
            "copilot": {
                "early_half": with_copilot.get("early_half"),
                "late_half": with_copilot.get("late_half"),
            },
        },
    }
    for label, row in ("baseline", baseline), ("copilot", with_copilot):
        report["scores"][f"{label}_return_pct"] = row.get("return_pct")
        report["scores"][f"{label}_win_rate_pct"] = row.get("win_rate_pct")
        report["scores"][f"{label}_profit_factor"] = row.get("profit_factor")
        report["scores"][f"{label}_max_dd_pct"] = row.get("max_drawdown_pct")

    cop_late = with_copilot.get("late_half") or {}
    base_late = baseline.get("late_half") or {}
    cop_early = with_copilot.get("early_half") or {}
    base_early = baseline.get("early_half") or {}
    late_wr_delta = (cop_late.get("win_rate_pct") or 0) - (base_late.get("win_rate_pct") or 0)
    early_wr_delta = (cop_early.get("win_rate_pct") or 0) - (base_early.get("win_rate_pct") or 0)
    report["hypothesis"]["late_wr_delta_vs_baseline"] = round(late_wr_delta, 1)
    report["hypothesis"]["early_wr_delta_vs_baseline"] = round(early_wr_delta, 1)
    report["hypothesis"]["copilot_late_catches_up"] = late_wr_delta > 2.0 and (
        (cop_late.get("pnl_usd") or 0) >= (base_late.get("pnl_usd") or 0) - 1.0
    )
    report["recommendation"] = (
        "Enable LLM_COPILOT_TRADING — copilot wins on balanced score"
        if winner == "copilot"
        else (
            "Keep copilot if learning curve improving; baseline wins overall"
            if winner == "baseline"
            else "Inconclusive — extend history or tune copilot strictness"
        )
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    log.info("Report: %s", REPORT_PATH)
    log.info(
        "RESULT winner=%s | baseline ret=%.1f%% wr=%.1f%% | copilot ret=%.1f%% wr=%.1f%% vetoes=%s",
        winner,
        baseline.get("return_pct", 0),
        baseline.get("win_rate_pct", 0),
        with_copilot.get("return_pct", 0),
        with_copilot.get("win_rate_pct", 0),
        with_copilot.get("copilot_vetoes", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
