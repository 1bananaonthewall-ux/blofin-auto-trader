#!/usr/bin/env python3
"""Single manual entry pass — micro-account friendly, no LLM latency."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("manual")

from account_guard import entry_allowed
from config import load_settings
from exchange_client import BlofinExchange
from margin_engine import MarginAwareSizer
from mission_config import sole_objective_label
from strategy import Signal
from ta_confluence import confluence_to_decision, run_all_analyses
from universe import load_tradeable_markets


def main() -> int:
    # config.load_dotenv(override=True) runs on import — set after imports, before load_settings()
    os.environ["LLM_TRADING_ENABLED"] = "false"
    settings = load_settings()
    log.info("mission: %s", sole_objective_label())
    log.info("mode=%s dry_run=%s", settings.mode, settings.dry_run)

    if settings.dry_run:
        log.error("DRY_RUN=true — set DRY_RUN=false in .env for live manual pass")
        return 1
    if settings.entries_paused:
        log.error("ENTRIES_PAUSED=true — cannot open")
        return 1

    ex = BlofinExchange(settings)
    ex.load()

    equity = ex.fetch_equity_usdt()
    free = ex.fetch_free_equity_usdt()
    positions = ex.fetch_all_positions()
    log.info("equity=$%.4f free=$%.4f open_positions=%d", equity, free, len(positions))

    if positions:
        for sym, p in positions.items():
            log.info(
                "  hold %s %s contracts=%s lev=%sx",
                sym,
                p.get("side"),
                p.get("contracts"),
                p.get("leverage"),
            )
        log.info("already in trade — steward/bot should manage; no duplicate entry")
        return 0

    ok, why = entry_allowed(settings, equity=equity, free_margin=free, open_count=0)
    if not ok:
        log.error("entry blocked: %s", why)
        return 1

    lev_cap = min(20, int(settings.scalp_leverage_max or 20))
    markets = load_tradeable_markets(ex, equity, lev_cap, 0.85, max_positions_cap=1)
    if not markets:
        log.error("no affordable markets for equity $%.2f", equity)
        return 1

    scan = markets[:18]
    log.info("scanning %d cheapest symbols (lev cap %dx)", len(scan), lev_cap)

    best = None
    for mkt in scan:
        try:
            ohlcv_1m = ex.fetch_ohlcv(mkt.symbol, "1m", 80)
            ohlcv_5m = ex.fetch_ohlcv(mkt.symbol, "5m", 40)
            funding = ex.fetch_funding_rate(mkt.symbol)
            cf = run_all_analyses(ohlcv_1m, ohlcv_5m, funding_rate=funding, ml_decision=None)
            if cf is None or len(cf.agreeing) < 2:
                continue
            dec = confluence_to_decision(cf)
            if dec.score < 40.0:
                continue
            conf = getattr(dec, "model_confidence", 0.0) or (dec.score / 100.0)
            if conf < 0.48:
                continue
        except Exception as exc:
            log.debug("analyze %s: %s", mkt.symbol, exc)
            continue
        if dec is None or dec.signal == Signal.FLAT:
            continue

        sym_cap = min(lev_cap, ex.symbol_leverage_cap(mkt.symbol) or lev_cap)
        conf = getattr(dec, "model_confidence", 0.0) or (dec.score / 100.0)
        sizer = MarginAwareSizer(
            free_margin=free,
            fee_taker=0.0006,
            fee_maker=0.0002,
            min_take_profit_pct=0.004,
            base_leverage=min(10, sym_cap),
            max_leverage=sym_cap,
            margin_reserve_usdt=max(0.05, settings.margin_reserve_usdt),
            risk_fraction=0.12,
            model_confidence=conf,
            liquidation_buffer=1.35,
            scalp_mode=True,
            max_stop_pct=0.025,
            max_take_pct=0.05,
            fee_coverage_multiple=2.0,
            margin_use_fraction=0.85,
            min_margin_rate=0.72,
            min_rr=1.35,
        )
        plan = sizer.plan_trade(
            dec.close,
            dec.stop_pct,
            dec.take_pct,
            mkt.contract_size,
            mkt.min_size,
            margin_fraction=0.35,
        )
        if plan is None:
            continue
        rank = conf * 100 + dec.score
        cand = (rank, mkt, dec, plan)
        if best is None or cand[0] > best[0]:
            best = cand
        log.info(
            "candidate %s %s conf=%.2f score=%.0f margin=$%.3f lev=%dx",
            mkt.symbol.split("/")[0],
            dec.signal.value,
            conf,
            dec.score,
            plan.margin_usd,
            plan.leverage,
        )
        time.sleep(0.15)

    if best is None:
        log.warning("no setup passed signal + sizing — try again later")
        return 2

    _, mkt, dec, plan = best
    log.info(
        "OPENING %s %s contracts=%s margin~$%.3f lev=%dx stop=%.2f%% take=%.2f%%",
        mkt.symbol,
        dec.signal.value,
        plan.contracts,
        plan.margin_usd,
        plan.leverage,
        plan.stop_pct * 100,
        plan.take_pct * 100,
    )
    result = ex.open_position(
        symbol=mkt.symbol,
        side=dec.signal.value,
        contracts=plan.contracts,
        stop_pct=plan.stop_pct,
        take_pct=plan.take_pct,
        dry_run=False,
        leverage=plan.leverage,
    )
    if result is None:
        err = getattr(ex, "last_open_error", "") or "unknown"
        log.error("open failed: %s", err)
        return 3

    time.sleep(0.5)
    positions = ex.fetch_all_positions()
    if mkt.symbol in positions:
        p = positions[mkt.symbol]
        log.info(
            "FILLED %s %s entry=%.6f margin=$%.3f lev=%sx",
            mkt.symbol,
            p.get("side"),
            float(p.get("entry_price") or 0),
            float(p.get("margin_usdt") or 0),
            p.get("leverage"),
        )
        return 0
    log.warning("order sent but position not visible yet — check Blofin app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
