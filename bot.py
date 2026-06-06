#!/usr/bin/env python3
"""
Blofin Autonomous Growth Engine
Target: mission_config sole objective — doctrine in autonomous_engine.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from account_guard import UNLIMITED_POSITIONS, effective_max_open, entry_allowed, same_side_exposure_ok
from autonomous_engine import AutonomousGrowthEngine, create_engine
from config import Settings, load_settings
from conviction import (
    margin_fraction_for_conviction,
    rank_llm_only_opens,
    rank_setups,
    select_conviction_ties,
)
from cooldowns import SymbolCooldowns
from symbol_side_guard import SymbolSideGuard
from entry_pacer import EntryPacer
from tpsl_pacing import TpslPacer, use_tpsl_only_pacing
from exchange_client import BlofinExchange
from journal import TradeJournal
from margin_engine import MarginAwareSizer
from ml.features import build_feature_vector
from ml.outcomes import TradeOutcomeTracker
from ml.predictor import MLPredictor
from ml.universe_trainer import ContinuousMlTrainer
from universe import training_symbol_cap
from signals import analyze_symbol
from strategy import Signal
from market_stream import BlofinMarketStream
from markets import symbol_to_inst_id
from dashboard_publish import publish_account_snapshot
from position_registry import PositionRegistry
from position_steward import PositionSteward
from scalp_optimizer import ScalpOptimizer, effective_cooldown_minutes, effective_entry_gap
from scan_orchestrator import ScanOrchestrator
from self_heal import SelfHealer
from symbol_quality import SymbolQualityStore
from universe import load_tradeable_markets
import api_backoff
from live_update import RuntimeCtx, create_reloader
from leverage_rotation import count_below_target_leverage
from position_brain import reconcile_open_book

log = logging.getLogger(__name__)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _load_performance_stats(state_dir: Path) -> tuple[float, float, int]:
    try:
        from roe_learning import get_roe_store

        wr, pf, streak, _avg = get_roe_store(state_dir).recent_performance(3600.0, limit=30)
        if (get_roe_store(state_dir)._data.get("global", {}).get("recent") or []):
            return wr, min(5.0, pf), streak
    except Exception:
        pass
    path = state_dir / "profitability.json"
    if not path.exists():
        return 0.5, 1.0, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trades = raw.get("trades", [])[-30:]
        if not trades:
            return 0.5, 1.0, 0
        roes = [float(t["roe_pct"]) for t in trades if t.get("roe_pct") is not None]
        if roes:
            wins = sum(1 for r in roes if r > 0)
            wr = wins / len(roes)
            pos = sum(r for r in roes if r > 0)
            neg = abs(sum(r for r in roes if r < 0))
            pf = (pos / neg) if neg > 0 else (2.0 if pos > 0 else 1.0)
            streak = 0
            for r in reversed(roes):
                if r < 0:
                    streak += 1
                else:
                    break
            return wr, min(5.0, pf), streak
        wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
        wr = wins / len(trades)
        gp = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
        gl = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
        pf = (gp / gl) if gl > 0 else (2.0 if gp > 0 else 1.0)
        streak = 0
        for t in reversed(trades):
            if t.get("net_pnl", 0) < 0:
                streak += 1
            else:
                break
        return wr, min(5.0, pf), streak
    except Exception:
        return 0.5, 1.0, 0


def _sync_ml_metrics(engine: AutonomousGrowthEngine, ml: MLPredictor, tracker: TradeOutcomeTracker | None) -> None:
    val_acc, long_p, short_p = 0.55, 0.5, 0.5
    fb = 0
    if ml.is_ready() and ml.model and ml.model.metrics:
        m = ml.model.metrics
        val_acc = m.val_accuracy
        long_p = m.val_long_precision
        short_p = m.val_short_precision
        fb = m.feedback_samples
    if tracker:
        mm = getattr(getattr(engine, "settings", None), "margin_mode", None)
        X, y = tracker.load_labelled_samples(max_samples=500, margin_mode=mm)
        if len(y) > 0:
            fb = max(fb, len(y))
    engine.set_ml_metrics(val_acc, long_p, short_p, fb)


def _snapshot_equity(state_dir: Path, equity: float, *, api_ok: bool = True) -> None:
    from equity_ticks import append_equity_tick

    append_equity_tick(state_dir, equity, api_ok=api_ok)


def _last_known_equity(state_dir: Path) -> tuple[float, float]:
    """Last non-zero equity/free from fluid_state or account_snapshot."""
    snap_path = state_dir / "account_snapshot.json"
    if snap_path.is_file():
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            eq = float(snap.get("equity") or 0)
            free = float(snap.get("free_margin") or 0)
            if eq > 0:
                return eq, free if free > 0 else eq
        except Exception:
            pass
    fluid_path = state_dir / "fluid_state.json"
    if fluid_path.is_file():
        try:
            fluid = json.loads(fluid_path.read_text(encoding="utf-8"))
            samples = fluid.get("samples") or []
            for item in reversed(samples):
                try:
                    val = float(item[1] if isinstance(item, (list, tuple)) else item)
                except (TypeError, ValueError, IndexError):
                    continue
                if val > 0:
                    return val, val * 0.85
            peak = float(fluid.get("peak_equity") or 0)
            if peak > 0:
                return peak, peak * 0.85
        except Exception:
            pass
    ticks_path = state_dir / "equity_ticks.jsonl"
    if ticks_path.is_file():
        try:
            lines = ticks_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines[-400:]):
                if not line.strip():
                    continue
                row = json.loads(line)
                val = float(row.get("equity") or 0)
                if val > 0:
                    return val, val * 0.85
        except Exception:
            pass
    return 0.0, 0.0


def try_open(
    ex: BlofinExchange,
    settings: Settings,
    engine: AutonomousGrowthEngine,
    knobs,
    symbol: str,
    decision,
    free_margin: float,
    equity: float,
    journal: TradeJournal,
    cooldowns: SymbolCooldowns,
    tracker: TradeOutcomeTracker | None,
    quality_store: SymbolQualityStore | None,
    registry: PositionRegistry,
    side_guard: SymbolSideGuard | None = None,
    *,
    conviction: float,
    margin_fraction: float,
    cooldown_seconds: int | None = None,
) -> bool:
    d = engine.doctrine
    if side_guard is not None:
        blocked, why = side_guard.is_flip_blocked(symbol, decision.signal.value)
        if blocked:
            log.info("side guard %s: %s", symbol, why)
            return False
    if cooldowns.is_blocked(symbol):
        log.info("skip %s: symbol cooldown", symbol.split("/")[0])
        return False
    ok_gate, gate_reason = engine.passes_signal_gate_with_reason(
        decision, knobs, rank_conviction=conviction
    )
    if not ok_gate:
        log.info("skip %s: %s", symbol.split("/")[0], gate_reason)
        return False

    open_positions = ex.fetch_all_positions()
    if symbol in open_positions:
        log.info("skip %s: position already open", symbol.split("/")[0])
        return False
    max_side = settings.max_same_side_positions
    ok_side, side_reason = same_side_exposure_ok(
        open_positions, decision.signal.value
    )
    if not ok_side:
        log.info("exposure gate %s: %s", symbol, side_reason)
        return False

    market = ex.market_for(symbol)
    if not market:
        log.info("skip %s: no market metadata", symbol.split("/")[0])
        return False
    if settings.scalp_3r_mode:
        cap = ex.symbol_leverage_cap(symbol)
        floor = min(40, int(settings.scalp_leverage_max))
        if cap < floor:
            log.debug("skip %s: exchange max %dx < %dx mission floor", symbol.split("/")[0], cap, floor)
            return False

    conf = getattr(decision, "model_confidence", 0.0) or (decision.score / 100.0)
    sp = engine.scalp
    from tpsl_policy import fast_lethal_cross_mode, skip_liq_guards_on_entry

    skip_liq_room = skip_liq_guards_on_entry(settings, decision=decision)
    sizer = MarginAwareSizer(
        free_margin=free_margin,
        fee_taker=d.fee_taker,
        fee_maker=d.fee_maker,
        min_take_profit_pct=sp.min_take_profit_pct if sp else d.min_take_profit_pct,
        base_leverage=sp.base_leverage if sp else d.base_leverage,
        max_leverage=min(
            ex.symbol_leverage_cap(symbol),
            (sp.max_leverage_cap if sp else knobs.max_leverage),
        ),
        margin_reserve_usdt=d.margin_reserve_usdt,
        risk_fraction=knobs.risk_per_trade_pct,
        model_confidence=conf,
        liquidation_buffer=d.liquidation_buffer_mult,
        scalp_mode=sp is not None,
        max_stop_pct=sp.max_stop_pct if sp else 0.08,
        max_take_pct=sp.max_take_pct if sp else 0.15,
        fee_coverage_multiple=sp.fee_coverage_multiple if sp else 2.0,
        margin_use_fraction=sp.margin_use_fraction if sp else settings.margin_use_fraction,
        min_margin_rate=settings.min_margin_rate,
        target_margin_rate=settings.target_margin_rate,
        max_effective_leverage=settings.max_effective_leverage,
        max_stop_liq_fraction=settings.max_stop_liq_fraction,
        min_rr=sp.min_rr if sp else 1.35,
        micro_equity_threshold=settings.micro_equity_threshold,
        small_account_threshold=settings.small_account_threshold,
        margin_top_up_enabled=settings.margin_top_up_enabled,
        skip_liq_room_check=skip_liq_room,
    )
    plan = sizer.plan_trade(
        decision.close,
        decision.stop_pct,
        decision.take_pct,
        market.contract_size,
        market.min_size,
        margin_fraction=margin_fraction,
        equity=equity,
    )
    if plan is None:
        log.info("skip %s: margin sizer returned no plan", symbol.split("/")[0])
        return False

    from dataclasses import replace

    from tpsl_policy import align_stop_take

    plan_stop, plan_take, tpsl_pol = align_stop_take(
        settings,
        plan.stop_pct,
        plan.take_pct,
        plan.leverage,
        decision=decision,
    )
    plan = replace(plan, stop_pct=plan_stop, take_pct=plan_take)

    from account_guard import universe_fill_active
    from hourly_3r import (
        hourly_3r_active,
        get_active_tuning_safe,
        is_opens_starved,
        is_wins_starved,
    )
    from liquidation_guard import achievable_margin_rates, liquidation_distance_pct, open_stop_within_liq_room

    tp_tune = get_active_tuning_safe()
    relax_stress_gates = getattr(settings, "entries_never_pause", False) or (
        hourly_3r_active(settings)
        and (is_wins_starved(settings, tp_tune) or is_opens_starved(settings, tp_tune))
    )

    min_mrate_open, _ = achievable_margin_rates(settings, equity)
    if plan.margin_rate < min_mrate_open - 0.01:
        log.info(
            "skip %s: margin rate %.0f%% < min %.0f%% for $%.2f equity",
            symbol.split("/")[0],
            plan.margin_rate * 100,
            min_mrate_open * 100,
            equity,
        )
        return False
    if not skip_liq_guards_on_entry(settings, decision=decision):
        liq_stop_frac = float(settings.max_stop_liq_fraction)
        if equity > 0 and equity < settings.micro_equity_threshold:
            liq_stop_frac = max(liq_stop_frac, 0.44)
        elif relax_stress_gates or universe_fill_active(settings):
            liq_stop_frac = max(liq_stop_frac, 0.40)
        if not open_stop_within_liq_room(
            plan.stop_pct,
            plan.leverage,
            max_fraction_of_liq_dist=liq_stop_frac,
        ):
            log.info(
                "skip %s: stop %.2f%% vs liq cap %.2f%% at %dx (frac=%.0f%%)",
                symbol.split("/")[0],
                plan.stop_pct * 100,
                liquidation_distance_pct(plan.leverage) * liq_stop_frac * 100,
                plan.leverage,
                liq_stop_frac * 100,
            )
            return False
    llm_only_entry = bool(getattr(settings, "llm_only_trading", False)) and (
        getattr(decision, "confluence_zone", "") == "cortex_llm"
    )
    mk_stress = float(getattr(decision, "markov_stress_p", 0.0) or 0.0)
    tier = getattr(decision, "winner_tier", "") or ""
    micro_acct = equity > 0 and equity < settings.micro_equity_threshold
    entry_relax = relax_stress_gates or universe_fill_active(settings) or micro_acct
    if micro_acct:
        stress_conv_min = 0.48
    elif relax_stress_gates:
        stress_conv_min = 0.46 if getattr(settings, "entries_never_pause", False) else 0.58
    elif entry_relax:
        stress_conv_min = 0.52
    else:
        stress_conv_min = 0.74
    if not llm_only_entry and mk_stress >= 0.36 and conviction < stress_conv_min:
        log.info(
            "skip %s: markov stress %.0f%% needs conv>=%.2f (have %.3f)",
            symbol.split("/")[0],
            mk_stress * 100,
            stress_conv_min,
            conviction,
        )
        return False
    stress_conf_min = 0.76 if relax_stress_gates else 0.82
    if (
        not llm_only_entry
        and mk_stress >= 0.36
        and tier == "good"
        and conf < stress_conf_min
        and not entry_relax
    ):
        log.info(
            "skip %s: good-tier in stress needs conf>=0.82 (have %.2f)",
            symbol.split("/")[0],
            conf,
        )
        return False
    if plan.margin_usd > free_margin - d.min_free_margin_usdt:
        log.info(
            "skip %s: margin $%.2f > budget $%.2f",
            symbol.split("/")[0],
            plan.margin_usd,
            max(0.0, free_margin - d.min_free_margin_usdt),
        )
        return False

    result = ex.open_position(
        symbol=symbol,
        side=decision.signal.value,
        contracts=plan.contracts,
        stop_pct=plan.stop_pct,
        take_pct=plan.take_pct,
        dry_run=settings.dry_run,
        leverage=plan.leverage,
    )
    if result is None and not settings.dry_run:
        err = getattr(ex, "last_open_error", "") or ""
        err_l = err.lower()
        if "102135" in err or "market is closed" in err_l:
            cooldowns.block(symbol, seconds=6 * 3600)
            log.warning("market closed %s — blocked 6h", symbol)
        elif "102115" in err or "delisted" in err_l or "will be delisted" in err_l:
            cooldowns.block(symbol, seconds=7 * 24 * 3600)
            log.warning("delisted %s — blocked 7d", symbol)
        elif "102087" in err or "maximum available position amount" in err_l:
            cooldowns.block(symbol, seconds=2 * 3600)
            log.warning(
                "exchange position cap %s — blocked 2h (size over coin limit)",
                symbol.split("/")[0],
            )
        log.info(
            "skip %s: order failed%s",
            symbol.split("/")[0],
            f" — {err}" if err else "",
        )
        return False
    if not settings.dry_run and result is not None:
        ex.ensure_leverage(symbol, decision.signal.value, leverage=plan.leverage)
        ex.ensure_margin_cushion(
            symbol,
            decision.signal.value,
            target_margin_rate=settings.target_margin_rate,
            dry_run=False,
        )
        pos_chk = ex._lookup_open_position(symbol, decision.signal.value)
        if pos_chk:
            live_mrate = float(pos_chk.get("margin_rate") or 0)
            min_mrate_live, _ = achievable_margin_rates(settings, equity)
            if live_mrate > 0 and live_mrate < min_mrate_live - 0.03:
                log.warning(
                    "abort %s: live margin rate %.0f%% < %.0f%% — closing (under-collateralized)",
                    symbol.split("/")[0],
                    live_mrate * 100,
                    min_mrate_live * 100,
                )
                ex.close_position(symbol, pos_chk, dry_run=False)
                registry.remove(symbol)
                return False
    if settings.dry_run:
        log.info("DRY_RUN open %s %s (no exchange order)", symbol, decision.signal.value)

    entry_px = decision.close
    if ex.stream:
        live = ex.stream.get_last_price(symbol)
        if live and live > 0:
            entry_px = live
    actual_entry = ex.fetch_position_entry_price(symbol) or entry_px
    if (
        quality_store is not None
        and settings.exec_slippage_penalty_enabled
        and not settings.dry_run
    ):
        bps = quality_store.note_slippage(symbol, expected_entry=entry_px, actual_entry=actual_entry)
        if bps is not None:
            quality_store.save()
            if bps >= settings.exec_slippage_warn_bps:
                log.warning(
                    "execution slippage %s %.1fbps (expected %.6f actual %.6f)",
                    symbol.split("/")[0],
                    bps,
                    entry_px,
                    actual_entry,
                )
    registry.record_open(
        symbol,
        side=decision.signal.value,
        entry_price=actual_entry,
        leverage=plan.leverage,
        stop_pct=plan.stop_pct,
        take_pct=plan.take_pct,
        conviction=conviction,
        margin_usdt=plan.margin_usd,
        contracts=plan.contracts,
        trade_style=tpsl_pol.style,
    )
    repaired = getattr(ex, "last_repaired_tpsl", None)
    if repaired:
        rep_stop, rep_take = repaired
        sl_px, tp_px = 0.0, 0.0
        prices = getattr(ex, "last_repaired_tpsl_prices", None)
        if prices and len(prices) >= 2:
            sl_px, tp_px = float(prices[0]), float(prices[1])
        registry.update_tpsl(
            symbol,
            stop_pct=rep_stop,
            take_pct=rep_take,
            sl_price=sl_px,
            tp_price=tp_px,
        )
        rr = rep_take / max(rep_stop, 1e-9)
        log.info(
            "3R TPSL live %s stop=%.2f%% take=%.2f%% rr=%.2f:1 (exchange liq)",
            symbol,
            rep_stop * 100,
            rep_take * 100,
            rr,
        )
    elif not settings.dry_run:
        log.warning(
            "OPEN %s: exchange TP/SL not confirmed on Blofin — immediate repair gate",
            symbol.split("/")[0],
        )
    if not settings.dry_run:
        live = ex.live_exchange_tpsl(symbol, decision.signal.value, actual_entry)
        if live is None:
            pos_now = ex._lookup_open_position(symbol, decision.signal.value)
            contracts_now = float((pos_now or {}).get("contracts") or plan.contracts)
            ok_rep, rep_stop, rep_take = ex.repair_position_tpsl(
                symbol,
                decision.signal.value,
                contracts_now,
                take_pct=plan.take_pct,
                configured_leverage=plan.leverage,
                dry_run=False,
                cancel_existing=False,
                registry_meta=registry.get(symbol) or {},
            )
            live2 = ex.live_exchange_tpsl(symbol, decision.signal.value, actual_entry)
            if ok_rep and live2 is not None:
                registry.update_tpsl(
                    symbol,
                    stop_pct=rep_stop,
                    take_pct=rep_take,
                    sl_price=live2.sl_price,
                    tp_price=live2.tp_price,
                )
            else:
                if pos_now is not None:
                    try:
                        ex.close_position(symbol, pos_now, dry_run=False)
                    except Exception:
                        log.exception("failed emergency close for unprotected position %s", symbol)
                registry.remove(symbol)
                cooldowns.block(symbol, seconds=120)
                log.error(
                    "OPEN ABORTED %s: unable to confirm exchange TP/SL after retries — closed to avoid naked risk",
                    symbol.split("/")[0],
                )
                return False
    if ex.stream:
        ex.stream.set_priority(
            [symbol_to_inst_id(s) for s in list(ex.fetch_all_positions().keys()) + [symbol]]
        )

    if tracker:
        try:
            ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", 60)
            ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", 40)
            feats = build_feature_vector(ohlcv_1m, ohlcv_5m, funding_rate=ex.fetch_funding_rate(symbol))
            if feats is not None:
                ep = decision.close
                sp = ep * (1 - plan.stop_pct) if decision.signal.value == "long" else ep * (1 + plan.stop_pct)
                tp = ep * (1 + plan.take_pct) if decision.signal.value == "long" else ep * (1 - plan.take_pct)
                curve = engine.curve_state
                tracker.record_entry(
                    symbol,
                    decision.signal.value,
                    ep,
                    sp,
                    tp,
                    feats.tolist(),
                    decision.score,
                    markov_state=str(getattr(decision, "markov_state", "")),
                    markov_stress_p=float(getattr(decision, "markov_stress_p", 0.0) or 0.0),
                    run_score=float(getattr(decision, "run_score", 0.0) or 0.0),
                    path_efficiency=float(getattr(decision, "path_efficiency", 0.0) or 0.0),
                    chop_index=float(getattr(decision, "chop_index", 0.0) or 0.0),
                    run_label=str(getattr(decision, "run_label", "") or ""),
                    pick_score=float(getattr(decision, "pick_score", 0.0) or 0.0),
                    curve_phase=curve.curve_phase if curve else "",
                    margin_mode=settings.margin_mode,
                )
        except Exception:
            pass

    if side_guard is not None:
        side_guard.record(symbol, decision.signal.value)

    journal.append(
        "open",
        symbol=symbol,
        side=decision.signal.value,
        confidence=conf,
        score=decision.score,
        contracts=plan.contracts,
        leverage=plan.leverage,
        margin=plan.margin_usd,
        req_daily_pct=knobs.required_daily_return_pct,
    )
    zone = getattr(decision, "confluence_zone", "")
    cf_pct = getattr(decision, "confluence_score", 0.0) * 100
    log.info(
        "BEST CONVICTION OPEN %s %s conv=%.3f conf=%.2f cf=%.0f%% zone=[%s] score=%.0f tier=%s lev=%d "
        "margin=$%.3f (%.1f%% free) fees_ok=$%.3f",
        symbol,
        decision.signal.value,
        conviction,
        conf,
        cf_pct,
        zone,
        decision.score,
        getattr(decision, "winner_tier", "") or "—",
        plan.leverage,
        plan.margin_usd,
        margin_fraction * 100,
        plan.profit_after_fees_usd,
    )
    return True


_scan_orchestrator = ScanOrchestrator()


def _scan_symbols_ws(
    ex: BlofinExchange,
    symbols: list[str],
    held: set[str],
    knobs,
    *,
    open_count: int = 0,
) -> list[str]:
    """Adaptive scan: momentum leaders + rotating slice through full universe."""
    if not symbols:
        return list(held)

    scan, plan = _scan_orchestrator.pick_symbols(
        symbols, held, ex.stream, knobs, open_count=open_count
    )

    log.info(
        "scan plan depth=%d/%d universe | momentum=%d rotation@%d fresh=%s cov=%.0f%%",
        plan.depth,
        plan.universe_n,
        plan.momentum_slots,
        plan.rotation_offset,
        plan.stream_fresh,
        plan.ticker_coverage * 100,
    )

    if ex.stream:
        pri_n = min(len(scan), max(60, plan.depth))
        pri = [symbol_to_inst_id(s) for s in scan[:pri_n]]
        ex.stream.set_priority(pri)
        ex.stream.set_ws_rotation_offset(
            _scan_orchestrator.advance_ws_rotation(
                [symbol_to_inst_id(s) for s in symbols], plan.depth
            )
        )
        boot_n = min(50, max(15, plan.depth // 6))
        for s in scan[:boot_n]:
            ex.stream.bootstrap_candles(s, "1m", 80)
            ex.stream.bootstrap_candles(s, "5m", 50)
    elif not ex.stream:
        extra = ex.next_scan_batch(
            [s for s in symbols if s not in held], max(plan.depth, knobs.symbols_per_tick)
        )
        for s in extra:
            if s not in scan:
                scan.append(s)
    return scan


def _count_unprotected_positions(
    ex: BlofinExchange,
    positions: dict,
    *,
    registry: PositionRegistry | None = None,
) -> tuple[int, list[str]]:
    missing: list[str] = []
    for key, pos in positions.items():
        symbol = str(pos.get("symbol") or key).split("#", 1)[0]
        side = str(pos.get("side") or "")
        entry = float(pos.get("entry_price") or 0)
        if not side or entry <= 0:
            continue
        if ex._lookup_open_position(symbol, side) is None:
            continue
        meta = (registry.get(symbol) if registry else None) or {}
        if (
            ex.live_exchange_tpsl(
                symbol,
                side,
                entry,
                pos=pos,
                registry_meta=meta,
                allow_registry_fallback=True,
            )
            is None
        ):
            missing.append(symbol)
    return len(missing), missing


def run_once(
    ex: BlofinExchange,
    settings: Settings,
    engine: AutonomousGrowthEngine,
    journal: TradeJournal,
    cooldowns: SymbolCooldowns,
    ml: MLPredictor,
    tracker: TradeOutcomeTracker | None,
    quality_store: SymbolQualityStore | None,
    pacer: EntryPacer,
    registry: PositionRegistry,
    side_guard: SymbolSideGuard,
    steward: PositionSteward | None = None,
    optimizer: ScalpOptimizer | None = None,
    tpsl_pacer: TpslPacer | None = None,
) -> int:
    equity = ex.fetch_equity_usdt()
    free_margin = ex.fetch_free_equity_usdt()
    if not ex.equity_fetch_ok:
        lk_eq, lk_free = _last_known_equity(settings.state_dir)
        if lk_eq > 0:
            equity = lk_eq
            free_margin = lk_free if lk_free > 0 else lk_eq
    engine.snapshot_equity(equity)
    try:
        from account_scale import maybe_capital_infusion

        maybe_capital_infusion(engine, settings, equity)
    except Exception:
        log.debug("capital infusion check failed", exc_info=True)
    _snapshot_equity(settings.state_dir, equity, api_ok=ex.equity_fetch_ok)

    wr, pf, loss_streak = _load_performance_stats(settings.state_dir)
    engine.record_performance(wr, pf, loss_streak)
    overseer = bool(getattr(settings, "llm_overseer_mode", False))
    if not getattr(settings, "llm_only_trading", False) or overseer:
        _sync_ml_metrics(engine, ml, tracker)

    if settings.trade_all_symbols:
        lev = engine.scalp.base_leverage if engine.scalp else engine.doctrine.base_leverage
        reload_markets = (
            not ex.markets
            or (time.time() - getattr(engine, "_markets_loaded_at", 0.0)) > 3600.0
        )
        if reload_markets and not api_backoff.is_paused():
            markets = load_tradeable_markets(ex, equity, lev, 0.95, 9999)
            ex.refresh_markets(markets)
            if markets:
                ex.save_markets_cache(settings.state_dir)
            engine._markets_loaded_at = time.time()
        elif reload_markets and api_backoff.is_paused():
            cached = ex.load_markets_from_cache(settings.state_dir)
            if cached:
                ex.markets = cached
                engine._markets_loaded_at = time.time()
        else:
            ex.patch_prices_from_stream()
        symbols = list(ex.markets.keys())
    else:
        symbols = [settings.symbol] if ex.market_for(settings.symbol) else []

    if steward:
        steward.run_once_now()
        free_margin = ex.fetch_free_equity_usdt()
        # Fresh exchange snapshot (steward may have just closed positions; avoid stale TP/SL gate)
        open_positions = ex.fetch_all_positions()
    else:
        open_positions = ex.fetch_all_positions()
        open_syms = {str(p.get("symbol") or k.split("#")[0]) for k, p in open_positions.items()}
        registry.sync_with_exchange(open_syms, api_ok=ex.positions_fetch_ok)
        publish_account_snapshot(
            settings.state_dir,
            equity,
            free_margin,
            open_positions,
            registry,
            api_ok=ex.equity_fetch_ok and ex.positions_fetch_ok,
        )
    unprotected_n, unprotected_syms = _count_unprotected_positions(
        ex, open_positions, registry=registry
    )
    if unprotected_n > 0 and not settings.dry_run:
        log.error(
            "exchange TP/SL missing on %d open position(s): %s — forcing repair before any new entries",
            unprotected_n,
            ", ".join(sorted({s.split("/")[0] for s in unprotected_syms})),
        )
        repaired_n = ex.repair_all_open_tpsl(settings, registry=registry)
        open_positions = ex.fetch_all_positions()
        unprotected_n, unprotected_syms = _count_unprotected_positions(
            ex, open_positions, registry=registry
        )
        if unprotected_n > 0:
            api_unreachable = api_backoff.is_paused() or not ex.tpsl_pending_fetch_ok
            if api_unreachable:
                log.warning(
                    "TP/SL unconfirmed on %d position(s) after repair (%d repaired) — "
                    "API/backoff unreachable; deferring hard-pause (registry trust active)",
                    unprotected_n,
                    repaired_n,
                )
            elif getattr(settings, "entries_never_pause", False):
                log.warning(
                    "TP/SL unconfirmed on %d position(s) — entries_never_pause: scanning continues",
                    unprotected_n,
                )
            else:
                log.error(
                    "TP/SL still missing on %d open position(s) after repair (%d repaired) — entries hard-paused",
                    unprotected_n,
                    repaired_n,
                )
                return max(6, min(15, int(getattr(settings, "scalp_steward_interval", 6))))
    engine.update_fluid(equity, free_margin, len(open_positions))
    if settings.markov_regime_enabled and (
        not getattr(settings, "llm_only_trading", False) or overseer
    ):
        anchor = "BTC/USDT:USDT"
        if anchor not in symbols and symbols:
            anchor = symbols[0]
        try:
            mk_ohlcv = ex.fetch_ohlcv(anchor, "1m", 120)
            engine.update_markov_global(mk_ohlcv, state_dir=settings.state_dir)
            mk = engine.markov_state
            if mk:
                log.info(mk.summary)
        except Exception:
            log.debug("markov global update failed", exc_info=True)
    opens_60m = optimizer.tuning.trades_last_hour if optimizer else 0
    if settings.throughput_brain_enabled:
        low_lev = count_below_target_leverage(
            open_positions, settings.scalp_leverage_max, registry
        )
        engine.evaluate_core(
            settings,
            equity=equity,
            free_margin=free_margin,
            opens_last_hour=opens_60m,
            open_count=len(open_positions),
            low_leverage_positions=low_lev,
        )
    tp = engine._last_throughput
    knobs = engine.compute_knobs(equity, free_margin, len(open_positions))
    engine.maybe_log_report(equity)

    log.info(
        "equity=$%.4f free=$%.4f open=%d | intensity=%.0f%% reliability=%.0f%% dd=%.1f%% | "
        "account_curve=%s vert=%.0f%% harvest=%.2fx | conf>=%.0f%% need=%.2f%%/day",
        equity,
        free_margin,
        len(open_positions),
        knobs.action_intensity * 100,
        knobs.path_reliability * 100,
        knobs.drawdown_pct,
        knobs.curve_phase,
        knobs.curve_verticality * 100,
        knobs.harvest_eagerness,
        knobs.min_confidence * 100,
        knobs.required_daily_return_pct,
    )
    if knobs.mission_directive:
        log.info("mission: %s | focus=%.0f%%", knobs.mission_directive, knobs.mission_focus * 100)
    if tp:
        log.info("core: %s", tp.directive)
    if knobs.drivers:
        log.info("manifold drivers: %s", " ".join(knobs.drivers))

    if free_margin < engine.doctrine.min_free_margin_usdt:
        return knobs.poll_seconds

    if not knobs.allow_new_entries:
        log.info(
            "entries paused — intensity %.0f%% reliability %.0f%% risk=%.2f%%; %d open",
            knobs.action_intensity * 100,
            knobs.path_reliability * 100,
            knobs.risk_per_trade_pct * 100,
            len(open_positions),
        )
        return knobs.poll_seconds

    ok, gate_reason = entry_allowed(
        settings,
        equity=equity,
        free_margin=free_margin,
        open_count=len(open_positions),
    )
    if not ok:
        log.info("entries gated: %s", gate_reason)
        return knobs.poll_seconds

    from account_guard import universe_fill_active

    fill_mode = universe_fill_active(settings)

    tpsl_pace = use_tpsl_only_pacing(settings) and tpsl_pacer is not None
    opens_allowed = True
    if tpsl_pace:
        wait = tpsl_pacer.seconds_until_ready()
        opens_allowed = tpsl_pacer.can_open()
        if not opens_allowed:
            log.debug(
                "TPSL pacer: %.1fs until next open (last=%s) — still scanning",
                wait,
                getattr(tpsl_pacer, "_last_kind", "") or "none",
            )
    else:
        if optimizer and settings.optimizer_enabled:
            gap = effective_entry_gap(settings, optimizer.tuning)
        else:
            gap = knobs.min_seconds_between_entries
        if tp:
            gap = min(gap, tp.target_entry_gap)
        pacer_floor = 2.0 if fill_mode else (8.0 if settings.throughput_brain_enabled else 15.0)
        pacer.set_min_seconds(gap, floor=pacer_floor)
        opens_allowed = pacer.can_enter()
        if not opens_allowed:
            log.debug(
                "entry pacer: %.0fs until next open — still scanning",
                pacer.seconds_until_ready(),
            )

    from hourly_3r import hourly_3r_active, is_entry_starved, is_opens_starved, is_wins_starved

    starved = tp.starved if tp else (
        optimizer is not None and is_entry_starved(settings, optimizer.tuning)
    )
    wins_starved = hourly_3r_active(settings) and is_wins_starved(
        settings, optimizer.tuning if optimizer else None
    )
    opens_starved = hourly_3r_active(settings) and is_opens_starved(
        settings, optimizer.tuning if optimizer else None
    )

    held = set(open_positions.keys())
    if overseer and symbols:
        try:
            from universe_rater import refresh_ratings

            refresh_ratings(ex, symbols, quality_store, settings.state_dir)
        except Exception:
            log.debug("universe rating refresh failed", exc_info=True)

    scan = _scan_symbols_ws(ex, symbols, held, knobs, open_count=len(open_positions))
    if overseer:
        try:
            from llm_overseer import load_directives
            from universe_rater import prioritize_scan

            scan = prioritize_scan(scan, settings.state_dir, load_directives(settings.state_dir).prefer)
        except Exception:
            log.debug("overseer scan prioritize failed", exc_info=True)

    try:
        from llm_overseer import maybe_run_overseer_tick

        maybe_run_overseer_tick(
            settings,
            knobs=knobs,
            ml=ml,
            opens_allowed=opens_allowed,
        )
    except Exception:
        log.debug("overseer tick failed", exc_info=True)

    llm_only = bool(getattr(settings, "llm_only_trading", False)) and not overseer
    candidates = []
    for sym in scan:
        try:
            if not llm_only:
                if getattr(settings, "trade_lessons_enabled", True):
                    try:
                        from trade_lessons import symbol_blocked

                        blocked, reason = symbol_blocked(settings, sym)
                        if blocked:
                            log.debug("skip %s: %s", sym.split("/")[0], reason)
                            continue
                    except Exception:
                        pass
                if (
                    quality_store is not None
                    and settings.symbol_quality_enabled
                    and not quality_store.allow(sym, settings.symbol_quality_floor)
                ):
                    continue
                if (
                    quality_store is not None
                    and getattr(settings, "runner_filter_enabled", True)
                    and quality_store.skip_choppy_symbol(sym, floor=getattr(settings, "runner_min_score", 0.48))
                ):
                    log.debug("skip %s: remembered choppy runner score", sym.split("/")[0])
                    continue
            scan_min_conf = knobs.min_confidence
            scan_min_score = knobs.min_signal_score
            if fill_mode and equity > 0 and equity < settings.micro_equity_threshold:
                scan_min_conf = min(scan_min_conf, 0.52)
                scan_min_score = min(scan_min_score, 52.0)
            elif fill_mode and (starved or opens_starved):
                scan_min_conf = min(scan_min_conf, 0.52)
                scan_min_score = min(scan_min_score, 50.0)
            elif getattr(settings, "entries_never_pause", False):
                scan_min_conf = min(scan_min_conf, 0.52)
                scan_min_score = min(scan_min_score, 50.0)
            d = analyze_symbol(
                ex,
                settings,
                sym,
                None if llm_only else ml,
                equity=equity,
                min_confidence=scan_min_conf,
                min_signal_score=scan_min_score,
            )
            if d and d.signal != Signal.FLAT and engine.passes_signal_gate(
                d, knobs, min_confidence=scan_min_conf, min_signal_score=scan_min_score
            ):
                if not llm_only and quality_store is not None:
                    quality_store.note_run_quality(
                        sym,
                        run_score=float(getattr(d, "run_score", 0.5) or 0.5),
                        label=str(getattr(d, "run_label", "mixed") or "mixed"),
                        is_runner=bool(getattr(d, "is_runner", False)),
                        is_choppy=bool(getattr(d, "is_choppy", False)),
                    )
                    sq = quality_store.score(sym)
                    try:
                        from roe_learning import get_roe_store

                        sq = max(0.0, min(1.0, sq + get_roe_store(settings.state_dir).symbol_score_delta(sym)))
                    except Exception:
                        pass
                    d.symbol_quality = sq
                    if settings.symbol_quality_enabled and sq < 0.35:
                        d.score = max(0.0, d.score - (0.35 - sq) * 12.0)
                candidates.append((sym, d))
            time.sleep(0.12 if llm_only else 0.05)
        except Exception:
            log.exception("scan %s", sym)

    max_open = effective_max_open(settings, equity)
    if max_open >= UNLIMITED_POSITIONS:
        per_tick = knobs.max_opens_per_tick
    else:
        open_slots = max(0, max_open - len(open_positions))
        per_tick = min(knobs.max_opens_per_tick, open_slots) if open_slots else 0
        if per_tick <= 0:
            return knobs.poll_seconds

    if llm_only:
        elite = rank_llm_only_opens(candidates, per_tick)
        ranked = elite
        if not opens_allowed:
            return knobs.poll_seconds
        if not elite:
            if candidates:
                log.info("no open: %d scanned, 0 LLM cortex_llm approvals", len(candidates))
            return knobs.poll_seconds
        log.info(
            "LLM-ONLY opens %d/%d | top %s conf=%.2f",
            len(elite),
            len(candidates),
            elite[0].symbol.split("/")[0],
            elite[0].confidence,
        )
        free_margin = ex.fetch_free_equity_usdt()
        if free_margin < engine.doctrine.min_free_margin_usdt:
            return knobs.poll_seconds
        cd_sec = 0 if tpsl_pace else int(
            effective_cooldown_minutes(settings, optimizer.tuning if optimizer else None) * 60
        )
        if optimizer and not tpsl_pace:
            cooldowns.cooldown_seconds = cd_sec
        opened = 0
        for setup in elite:
            free_margin = ex.fetch_free_equity_usdt()
            if free_margin < engine.doctrine.min_free_margin_usdt:
                break
            margin_frac = margin_fraction_for_conviction(
                setup.conviction,
                setup.confidence,
                base_pct=knobs.margin_deploy_base_pct,
                max_pct=knobs.margin_deploy_max_pct,
                action_intensity=knobs.action_intensity,
                tie_count=1,
                loss_streak=engine._consecutive_losses,
            )
            if equity < settings.micro_equity_threshold:
                margin_frac = max(margin_frac, min(0.85, settings.micro_max_margin_frac * 3.0))
            try:
                if try_open(
                    ex,
                    settings,
                    engine,
                    knobs,
                    setup.symbol,
                    setup.decision,
                    free_margin,
                    equity,
                    journal,
                    cooldowns,
                    tracker,
                    quality_store,
                    registry,
                    side_guard,
                    conviction=setup.conviction,
                    margin_fraction=margin_frac,
                    cooldown_seconds=cd_sec,
                ):
                    opened += 1
                    if not tpsl_pace and cd_sec > 0:
                        cooldowns.block(setup.symbol, seconds=cd_sec)
                    open_positions[setup.symbol] = {"side": setup.decision.signal.value}
                    if tpsl_pacer:
                        tpsl_pacer.note_open(setup.symbol, setup.decision.signal.value)
            except Exception:
                log.exception("open %s", setup.symbol)
        if opened:
            if not tpsl_pace:
                pacer.record_entry()
            log.info("opened %d position(s) (llm_only)", opened)
        return knobs.poll_seconds

    ranked = rank_setups(
        candidates,
        knobs.path_reliability,
        mission_scale=engine.mission_scale_conviction,
    )
    entry_press = (
        fill_mode
        or starved
        or opens_starved
        or wins_starved
        or (settings.winner_only_mode and getattr(settings, "entries_never_pause", False))
    )
    mission_floor = knobs.min_confidence
    if settings.llm_trading_enabled:
        mission_floor = min(mission_floor, settings.llm_trading_min_confidence + 0.05)
    if entry_press:
        if settings.winner_only_mode and getattr(settings, "entries_never_pause", False):
            mission_floor = min(mission_floor, 0.44)
        if wins_starved and not opens_starved:
            mission_floor = min(
                max(mission_floor, 0.44, knobs.min_confidence * 0.78),
                0.52,
            )
        else:
            mission_floor = min(
                max(mission_floor, 0.40, knobs.min_confidence * 0.72),
                0.48,
            )
    if engine._last_mission is not None and not settings.unrestricted_trading and not settings.entries_never_pause:
        if not entry_press:
            mission_floor = max(mission_floor, engine._last_mission.min_conviction)
        else:
            mission_floor = min(
                mission_floor,
                max(0.46, engine._last_mission.min_conviction - 0.10),
            )
    allow_apex_fallback = (
        (tp.allow_elite_fallback if tp else starved)
        or not settings.winner_apex_preferred
        or entry_press
    )
    elite = select_conviction_ties(
        ranked,
        max_opens=per_tick,
        min_conviction=mission_floor,
        apex_preferred=settings.winner_apex_preferred and not entry_press,
        elite_only=settings.winner_elite_only and not entry_press,
        allow_elite_fallback=allow_apex_fallback,
    )
    if not elite and ranked and entry_press and settings.winner_only_mode:
        from scalp_optimizer import micro_tune_for_flow

        top = ranked[0]
        tuned_floor, tune_note = micro_tune_for_flow(
            settings.state_dir,
            settings,
            ranked_count=len(ranked),
            top_conviction=top.conviction,
            top_tier=getattr(top.decision, "winner_tier", "") or "",
            top_winner_score=float(getattr(top.decision, "winner_score", 0.0) or 0.0),
            mission_floor=mission_floor,
        )
        if tuned_floor < mission_floor:
            mission_floor = tuned_floor
            elite = select_conviction_ties(
                ranked,
                max_opens=per_tick,
                min_conviction=mission_floor,
                apex_preferred=settings.winner_apex_preferred and not entry_press,
                elite_only=settings.winner_elite_only and not entry_press,
                allow_elite_fallback=allow_apex_fallback,
            )
            if elite and tune_note:
                log.info(
                    "flow self-tune: conv floor %.2f -> %.2f (%s) | open %s conv=%.3f",
                    knobs.min_confidence,
                    mission_floor,
                    tune_note,
                    elite[0].symbol.split("/")[0],
                    elite[0].conviction,
                )
    if not opens_allowed:
        return knobs.poll_seconds

    if not elite:
        if ranked:
            top = ranked[0]
            want = (
                "apex"
                if settings.winner_apex_preferred and not entry_press
                else ("good+" if entry_press else "elite+")
            )
            log.info(
                "no open: top conv=%.3f tier=%s candidates=%d (need %s conv>=%.2f) | best=%s",
                top.conviction,
                getattr(top.decision, "winner_tier", ""),
                len(ranked),
                want,
                mission_floor,
                top.symbol,
            )
        elif candidates:
            log.info("no open: %d raw candidates failed conviction rank", len(candidates))
        return knobs.poll_seconds

    top_conv = elite[0].conviction
    if len(elite) > 1:
        tie_syms = ", ".join(f"{e.symbol} {e.conviction:.3f}" for e in elite)
        zones = ", ".join(
            f"{e.symbol.split('/')[0]}[{getattr(e.decision, 'confluence_zone', '')}]"
            for e in elite[:3]
        )
        log.info(
            "conviction TIE %d-way top=%.3f | %s | scanned=%d",
            len(elite),
            top_conv,
            zones or tie_syms,
            len(ranked),
        )
    else:
        log.info(
            "conviction #1 %s pick=%.2f fast=%.2f conv=%.3f | scanned=%d",
            elite[0].symbol,
            getattr(elite[0].decision, "pick_score", 0.0),
            getattr(elite[0].decision, "fast_win_score", 0.0),
            elite[0].conviction,
            len(ranked),
        )
    if len(ranked) > len(elite):
        log.info(
            "next below tie: %s %.3f (gap %.4f)",
            ranked[len(elite)].symbol,
            ranked[len(elite)].conviction,
            top_conv - ranked[len(elite)].conviction,
        )

    free_margin = ex.fetch_free_equity_usdt()
    if free_margin < engine.doctrine.min_free_margin_usdt:
        return knobs.poll_seconds

    if not tpsl_pace:
        cd_sec = int(effective_cooldown_minutes(settings, optimizer.tuning if optimizer else None) * 60)
        if optimizer:
            cooldowns.cooldown_seconds = cd_sec
    else:
        cd_sec = 0

    opened = 0
    tie_n = len(elite)
    for setup in elite:
        free_margin = ex.fetch_free_equity_usdt()
        if free_margin < engine.doctrine.min_free_margin_usdt:
            break
        margin_frac = margin_fraction_for_conviction(
            setup.conviction,
            setup.confidence,
            base_pct=knobs.margin_deploy_base_pct,
            max_pct=knobs.margin_deploy_max_pct,
            action_intensity=knobs.action_intensity,
            tie_count=tie_n,
            loss_streak=engine._consecutive_losses,
        )
        if equity < settings.micro_equity_threshold:
            margin_frac = max(margin_frac, min(0.85, settings.micro_max_margin_frac * 3.0))
        elif equity >= settings.small_account_threshold:
            margin_frac = min(
                0.28,
                settings.margin_use_fraction * 0.38,
                margin_frac * 1.4,
            )
        elif equity >= 25.0:
            margin_frac = min(0.22, margin_frac * 1.2)
        try:
            if try_open(
                ex,
                settings,
                engine,
                knobs,
                setup.symbol,
                setup.decision,
                free_margin,
                equity,
                journal,
                cooldowns,
                tracker,
                quality_store,
                registry,
                side_guard,
                conviction=setup.conviction,
                margin_fraction=margin_frac,
                cooldown_seconds=cd_sec,
            ):
                opened += 1
                if not tpsl_pace and cd_sec > 0:
                    cooldowns.block(setup.symbol, seconds=cd_sec)
                open_positions[setup.symbol] = {"side": setup.decision.signal.value}
                if not tpsl_pace:
                    tier = getattr(setup.decision, "winner_tier", "")
                    if tier in ("apex", "elite"):
                        gap_s = (
                            settings.winner_elite_entry_gap_seconds - 4.0
                            if tier == "apex"
                            else settings.winner_elite_entry_gap_seconds
                        )
                        pacer.set_min_seconds(gap_s, floor=8.0)
        except Exception:
            log.exception("open %s", setup.symbol)

    if opened and not tpsl_pace:
        pacer.record_entry()
        tiers = "+".join(sorted({getattr(s.decision, "winner_tier", "?") for s in elite}))
        log.info("opened %d/%d tied %s conviction(s) this cycle", opened, tie_n, tiers)

    return knobs.poll_seconds


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(settings.state_dir)
    engine.bind_settings(settings)
    engine.unrestricted_trading = settings.unrestricted_trading
    engine.entries_never_pause = settings.entries_never_pause
    if settings.unrestricted_trading:
        log.warning(
            "UNRESTRICTED_TRADING=true — drawdown/mission/fluid entry pauses DISABLED (SL/TP still on)"
        )
    elif settings.entries_never_pause:
        log.warning(
            "ENTRIES_NEVER_PAUSE=true — mission/fluid/curve entry pauses OFF; optimizers still tune quality"
        )
    log.info("AUTONOMOUS ENGINE: %s", engine.doctrine_summary())

    if getattr(settings, "llm_overseer_mode", False):
        log.warning(
            "LLM OVERSEER: 1.5B supervises ML swarm | optimize every %ds | "
            "instant universe ratings | autocode + blocker fixes",
            getattr(settings, "overseer_interval_seconds", 300),
        )
    if settings.llm_trading_enabled or getattr(settings, "llm_overseer_mode", False):
        try:
            from local_llm import resolve_provider, status_line, warmup_provider

            provider = resolve_provider()
            if getattr(settings, "llm_only_trading", False) and not getattr(
                settings, "llm_overseer_mode", False
            ):
                log.warning(
                    "LLM-ONLY TRADING: local LLM is the sole entry brain — "
                    "no winner/ML/pick gate after policy (steward/TP/SL unchanged)"
                )
                if provider == "none":
                    log.error(
                        "LLM_ONLY_TRADING=true but no LLM backend — "
                        "run scripts\\enable_llm_only_1.5b.ps1 then restart-fresh"
                    )
                else:
                    log.warning("LLM-ONLY model: %s | provider=%s", status_line(), provider)
            log.info(
                "CORTEX TRADING BRAIN on — provider=%s | %s | cache=%.0fs",
                provider,
                status_line(),
                settings.llm_policy_cache_sec,
            )
            log.info("LLM warmup: %s", warmup_provider())
        except Exception as exc:
            log.warning("LLM warmup failed: %s", exc)

    ex = BlofinExchange(settings)
    ex.load()
    cross_migrator = None
    if settings.mode == "live" and not settings.dry_run:
        from margin_migrator import CrossMarginAutoMigrator

        cross_migrator = CrossMarginAutoMigrator(settings.state_dir, settings)
        try:
            ex.ensure_account_margin_mode()
        except Exception as exc:
            log.warning("margin mode sync failed: %s", exc)
        try:
            n_tpsl = ex.repair_all_open_tpsl(settings)
            if n_tpsl:
                log.warning("STARTUP TPSL sweep: repaired %d open position(s)", n_tpsl)
        except Exception as exc:
            log.warning("STARTUP TPSL sweep failed: %s", exc)
    eq0 = ex.fetch_equity_usdt()
    if not ex.equity_fetch_ok:
        lk_eq, lk_free = _last_known_equity(settings.state_dir)
        if lk_eq > 0:
            eq0 = lk_eq
            ex._cached_equity = lk_eq
            ex._cached_free = lk_free if lk_free > 0 else lk_eq
            log.info("startup equity from state cache: $%.4f", eq0)
    cap_label = (
        "unlimited (margin only)"
        if effective_max_open(settings, eq0) >= UNLIMITED_POSITIONS
        else str(effective_max_open(settings, eq0))
    )
    log.info(
        "account guard: max_open=%s (equity<$%.0f) | opens/tick=%d | min_free=%.0f%% | entries_paused=%s",
        cap_label,
        settings.small_account_threshold,
        settings.max_opens_per_tick,
        settings.small_account_min_free_pct * 100,
        settings.entries_paused,
    )
    if settings.scalp_3r_mode:
        log.warning(
            "3R SCALP PROFILE: min_rr=%.1f | harvest>=%.1fR | lev=%d-%dx | "
            "hard SL (losses realized) | TP recomputed from exchange liq SL",
            settings.scalp_3r_min_rr,
            settings.scalp_3r_harvest_min_r,
            settings.scalp_leverage,
            settings.scalp_leverage_max,
        )
    if settings.winner_only_mode:
        cap = effective_max_open(settings, eq0)
        cap_msg = "unlimited" if cap >= UNLIMITED_POSITIONS else str(cap)
        tier_note = (
            f"apex>={settings.winner_apex_score:.2f} preferred"
            if settings.winner_apex_preferred
            else f"good+ winner score>={settings.winner_min_score:.2f} (endless universe scan)"
        )
        log.warning(
            "WINNERS ONLY: %s | elite>=%.2f | target %d–%d opens/hr | lev=%d–%dx | "
            "max_open=%s | core_brain=on | hold/SL/TP unchanged",
            tier_note,
            settings.winner_elite_score,
            settings.optimizer_target_min_tph,
            settings.optimizer_target_max_tph,
            settings.scalp_leverage,
            settings.scalp_leverage_max,
            cap_msg,
        )
    if getattr(settings, "hourly_3r_winner_mode", False):
        log.warning(
            "HOURLY 3R WINNER MODE: fixed %.1f:1 TP/SL | target >=%d win(s)/hr | >=%d opens/hr | LLM off-path",
            settings.scalp_3r_min_rr,
            settings.optimizer_target_min_wins_per_hour,
            settings.optimizer_target_min_tph,
        )
    from account_guard import universe_fill_active as _ufa

    if _ufa(settings):
        log.warning(
            "UNIVERSE FILL: no hourly open cap | up to %d opens/cycle | fills until free margin — margin_rate anti-liq",
            settings.max_opens_per_tick,
        )
    if use_tpsl_only_pacing(settings):
        log.warning(
            "TPSL-ONLY PACING: scan always on | open gap TP=%.0fs SL=%.0fs | fast 3R stop<=%.2f%% take~%.2f%%",
            settings.tpsl_pace_gap_after_tp_seconds,
            settings.tpsl_pace_gap_after_sl_seconds,
            settings.scalp_max_stop_pct * 100,
            settings.scalp_max_stop_pct * settings.scalp_3r_min_rr * 100,
        )
    if getattr(settings, "momentum_wave_mode", False) and not getattr(
        settings, "hourly_3r_winner_mode", False
    ):
        log.warning(
            "MOMENTUM WAVE MODE: concurrent runners on | target >=%.0f%% leveraged winners | pace target >=%.1f%%/day",
            settings.momentum_wave_target_levered_profit_pct,
            settings.momentum_wave_target_daily_pct,
        )

    if settings.trade_all_symbols:
        lev = engine.scalp.base_leverage if engine.scalp else engine.doctrine.base_leverage
        sizing_eq = eq0 if eq0 > 0 else _last_known_equity(settings.state_dir)[0]
        if api_backoff.is_paused():
            cached = ex.load_markets_from_cache(settings.state_dir)
            if cached:
                ex.markets = cached
            mkts = list(ex.markets.values())
            if not mkts:
                log.warning(
                    "API paused (%.0fs) — no markets cache; stream will start with 0 instruments",
                    api_backoff.seconds_left(),
                )
        else:
            mkts = load_tradeable_markets(ex, sizing_eq, lev, 0.95, 9999)
        if mkts:
            ex.refresh_markets(mkts)
            ex.save_markets_cache(settings.state_dir)
        inst_ids = [m.inst_id for m in mkts]
    else:
        inst_ids = [symbol_to_inst_id(settings.symbol)]

    stream = BlofinMarketStream(ex.http, demo=settings.mode == "demo")
    stream.start(inst_ids)
    ex.attach_stream(stream)
    h = stream.stream_health()
    log.info(
        "websocket + REST ticker hub | %d assets | ws_live=%s ticker_cov=%.0f%%",
        len(inst_ids),
        h.get("ws_live"),
        float(h.get("ticker_coverage", 0)) * 100,
    )

    journal = TradeJournal(settings.state_dir / "trades.jsonl")
    registry = PositionRegistry(settings.state_dir)

    if cross_migrator and cross_migrator.enabled():
        try:
            n_cross = cross_migrator.run(ex, registry, force=True, max_per_pass=8)
            if n_cross:
                log.warning(
                    "STARTUP cross migrate: moved %d position(s) to cross margin",
                    n_cross,
                )
        except Exception as exc:
            log.warning("startup cross margin migrate failed: %s", exc)

    if settings.leverage_auto_upgrade:
        reconcile_open_book(ex, settings, registry, engine, max_closes_per_pass=2)

    cd_min = (
        settings.scalp_cooldown_minutes if settings.scalp_mode else engine.doctrine.symbol_cooldown_minutes
    )
    cooldowns = SymbolCooldowns(settings.state_dir / "cooldowns.json", cd_min * 60)
    engine.bind_exit_cooldowns(cooldowns)
    side_guard = SymbolSideGuard(
        settings.state_dir,
        block_seconds=settings.symbol_flip_block_minutes * 60.0,
    )
    pacer = EntryPacer(settings.state_dir, engine.doctrine.min_entry_gap_seconds)
    tpsl_pacer = (
        TpslPacer(settings.state_dir, settings) if use_tpsl_only_pacing(settings) else None
    )
    if tpsl_pacer is not None:
        engine.bind_tpsl_pacer(tpsl_pacer)
    ml = MLPredictor(
        settings.state_dir,
        min_confidence=engine.doctrine.min_confidence_floor,
        min_score=engine.doctrine.min_signal_score_default,
    )
    tracker = TradeOutcomeTracker(settings.state_dir, max_samples=settings.ml_real_feedback_max_samples)
    quality_store = SymbolQualityStore(settings.state_dir)

    def _harvest_eagerness() -> float:
        c = engine.curve_state
        base = c.harvest_eagerness if c else 1.0
        boost = 1.0
        try:
            from roe_learning import get_roe_store

            _wr, _pf, neg_streak, avg_roe = get_roe_store(settings.state_dir).recent_performance(
                3600.0, limit=24
            )
            if neg_streak >= 2 or avg_roe < -4.0:
                boost = min(1.45, 1.0 + neg_streak * 0.08)
        except Exception:
            pass
        if getattr(settings, "account_curve_maximize", True):
            if c and c.curve_phase in ("vertical", "climbing"):
                return min(0.65, base * 0.82 * boost)
            if c and c.curve_phase == "flat":
                return min(0.82, base * 0.92 * boost)
        if getattr(settings, "stack_winners_mode", True):
            return min(1.15, base * boost)
        return min(1.5, base * boost)

    if cross_migrator is None and settings.mode == "live" and not settings.dry_run:
        from margin_migrator import CrossMarginAutoMigrator

        cross_migrator = CrossMarginAutoMigrator(settings.state_dir, settings)
    if cross_migrator and cross_migrator.enabled():
        log.info(
            "auto cross-margin migrator on (every %.0fs, 1 position/pass)",
            cross_migrator._interval(),
        )

    steward = PositionSteward(
        ex,
        settings,
        engine,
        registry,
        tracker,
        harvest_eagerness_fn=_harvest_eagerness,
        cross_migrator=cross_migrator,
    )
    steward.start()

    llm_only_mode = bool(getattr(settings, "llm_only_trading", False)) and not bool(
        getattr(settings, "llm_overseer_mode", False)
    )
    ml_trainer: ContinuousMlTrainer | None = None
    if not llm_only_mode and settings.signal_mode == "ml" and settings.ml_continuous_train:

        def _on_ml_updated() -> None:
            ml.reload()
            _sync_ml_metrics(engine, ml, tracker)
            engine.mark_retrained()
            if ml.is_ready():
                log.info("ML model live — %s", ml.metrics_summary())

        ml_trainer = ContinuousMlTrainer(ex, settings, on_model_updated=_on_ml_updated)
        ml_trainer.start()
    elif llm_only_mode:
        log.warning(
            "LLM-ONLY: ML continuous train OFF | winner/hourly-3r/markov entry brains disabled"
        )
    elif getattr(settings, "llm_overseer_mode", False):
        log.warning(
            "LLM OVERSEER: ML continuous train ON | 5m LLM optimize + instant universe ratings"
        )

    from stack_learning import run_startup_learning

    run_startup_learning(settings, ml, ml_trainer, tracker)

    healer = SelfHealer(settings.state_dir, enabled=settings.self_heal_enabled)
    optimizer = ScalpOptimizer(settings.state_dir, settings)
    log.warning(
        "STARTUP: 3R scalp + winner-only + 15m optimizer (all in-process, single process)"
    )
    if getattr(settings, "account_curve_maximize", True):
        log.warning(
            "ACCOUNT CURVE MAXIMIZE ON — entries and harvest tuned to steepen dashboard account curve"
        )
    if getattr(settings, "runner_filter_enabled", True):
        log.warning(
            "RUNNER FILTER ON — prefer steady directional coins; skip choppy up/down (run>=%.2f chop<=%.0f%%)",
            getattr(settings, "runner_min_score", 0.48),
            getattr(settings, "runner_max_chop", 0.56) * 100,
        )
    if settings.optimizer_enabled:
        log.warning(
            "15m OPTIMIZER ON | target %d–%d trades/hr | interval=%ds | no pause rails",
            settings.optimizer_target_min_tph,
            settings.optimizer_target_max_tph,
            int(settings.optimizer_interval_seconds),
        )
    if not llm_only_mode and settings.signal_mode == "ml" and not ml.is_ready():
        if ml_trainer:
            log.info(
                "ML warming up — auto-refit bootstrapping %d symbols (confluence until deploy)",
                settings.ml_bootstrap_symbols,
            )
            healer.mark_refit_requested()
        else:
            log.info("ML not deployed — enable ML_CONTINUOUS_TRAIN (auto-refit on startup)")
    if getattr(settings, "cortex_auto_train", True):
        log.warning(
            "CORTEX AUTO-TRAIN ON — knowledge.md rebuilds on startup + every %dm or after new closes",
            getattr(settings, "cortex_train_interval_minutes", 15),
        )
    from margin_mode import is_cross_margin, normalize_margin_mode

    mm = normalize_margin_mode(settings.margin_mode)
    if is_cross_margin(mm):
        log.warning(
            "CROSS MARGIN — shared wallet collateral | max eff lev %dx | "
            "TPSL repair min rate 100%% | pre-liq exit at %.0f%% buffer",
            settings.max_effective_leverage,
            settings.pre_liquidation_exit_factor * 100,
        )
    elif settings.margin_top_up_enabled:
        log.warning(
            "ISOLATED MARGIN CUSHION — target rate %.0f%% min %.0f%% | max eff lev %dx | "
            "adds margin after fill + steward top-up | pre-liq exit at %.0f%% buffer",
            settings.target_margin_rate * 100,
            settings.min_margin_rate * 100,
            settings.max_effective_leverage,
            settings.pre_liquidation_exit_factor * 100,
        )
    else:
        log.warning(
            "ISOLATED MARGIN — target rate %.0f%% min %.0f%% | max eff lev %dx | "
            "no margin top-up API | pre-liq exit at %.0f%% buffer",
            settings.target_margin_rate * 100,
            settings.min_margin_rate * 100,
            settings.max_effective_leverage,
            settings.pre_liquidation_exit_factor * 100,
        )

    reloader = create_reloader(settings)
    runtime = RuntimeCtx(
        settings=settings,
        engine=engine,
        steward=steward,
        ml_trainer=ml_trainer,
        optimizer=optimizer,
        ml=ml,
        healer=healer,
        ex=ex,
    )
    if reloader:
        log.warning(
            "LIVE UPDATE ON | poll=%.0fs | edit .py/.env locally — no restart needed for gates/signals",
            settings.live_update_poll_seconds,
        )

    poll = engine.doctrine.poll_seconds_base
    while True:
        try:
            if reloader and reloader.maybe_reload(runtime):
                settings = runtime.settings
                reloader.poll_seconds = settings.live_update_poll_seconds
                reloader.git_pull = settings.live_update_git_pull
                reloader.git_interval_seconds = settings.live_update_git_interval_seconds
                if runtime.ex is not None:
                    ex = runtime.ex
            eq = ex.fetch_equity_usdt()
            fm = ex.fetch_free_equity_usdt()
            wr, pf, streak = _load_performance_stats(settings.state_dir)
            engine.record_performance(wr, pf, streak)
            open_n = len(ex.fetch_all_positions())
            _sync_ml_metrics(engine, ml, tracker)
            engine.update_fluid(eq, fm, open_n)
            knobs = engine.compute_knobs(eq, fm, open_n)
            healer.tick(engine, settings, eq, fm, knobs, ml, ml_trainer)
            if open_n > 0:
                healer.heal_open_tpsl(ex, settings, ex.fetch_all_positions())
            optimizer.maybe_optimize(
                eq,
                win_rate=wr,
                profit_factor=pf,
                ml_ready=ml.is_ready(),
            )
            if settings.throughput_brain_enabled:
                positions = ex.fetch_all_positions()
                opens_60m = optimizer.tuning.trades_last_hour if optimizer else 0
                engine.evaluate_core(
                    settings,
                    equity=eq,
                    free_margin=fm,
                    opens_last_hour=opens_60m,
                    open_count=len(positions),
                    low_leverage_positions=count_below_target_leverage(
                        positions, settings.scalp_leverage_max, registry
                    ),
                )
                if engine.core.should_run_book_pass(len(positions)):
                    reconcile_open_book(
                        ex,
                        settings,
                        registry,
                        engine,
                        max_closes_per_pass=1,
                        tracker=tracker,
                    )
            if ml_trainer and tracker:
                ml_trainer.maybe_refit_from_outcomes(tracker)
            try:
                from stack_learning import maybe_periodic_learning

                maybe_periodic_learning(settings, ml, ml_trainer, tracker)
            except Exception:
                log.debug("periodic learning tick failed", exc_info=True)
            if healer.should_request_refit(engine, settings, knobs) and settings.signal_mode == "ml":
                log.info("autonomous ML refit triggered (throttled, background)")
                if ml_trainer:
                    ml_trainer.request_full_refit()
                elif healer.allow_subprocess_train():
                    subprocess.run(
                        [sys.executable, str(Path(__file__).parent / "train_model.py")],
                        check=False,
                    )
                    healer.mark_subprocess_train()
                    ml.reload()
                    engine.mark_retrained()
            poll = run_once(
                ex,
                settings,
                engine,
                journal,
                cooldowns,
                ml,
                tracker,
                quality_store,
                pacer,
                registry,
                side_guard,
                steward,
                optimizer,
                tpsl_pacer,
            )
        except Exception:
            log.exception("tick failed")
        time.sleep(poll)


if __name__ == "__main__":
    main()
