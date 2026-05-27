#!/usr/bin/env python3
"""
Blofin Autonomous Growth Engine
Target: $95M by 2027-09-01 — doctrine in autonomous_engine.py / mission_config.py
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
    rank_setups,
    select_conviction_ties,
)
from cooldowns import SymbolCooldowns
from symbol_side_guard import SymbolSideGuard
from entry_pacer import EntryPacer
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
from position_registry import PositionRegistry
from position_steward import PositionSteward
from scalp_optimizer import ScalpOptimizer, effective_cooldown_minutes, effective_entry_gap
from scan_orchestrator import ScanOrchestrator
from self_heal import SelfHealer
from universe import load_tradeable_markets
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
    path = state_dir / "profitability.json"
    if not path.exists():
        return 0.5, 1.0, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trades = raw.get("trades", [])[-30:]
        if not trades:
            return 0.5, 1.0, 0
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
        X, y = tracker.load_labelled_samples(max_samples=500)
        if len(y) > 0:
            fb = max(fb, len(y))
    engine.set_ml_metrics(val_acc, long_p, short_p, fb)


def _snapshot_equity(state_dir: Path, equity: float) -> None:
    path = state_dir / "equity_ticks.jsonl"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "equity": round(equity, 6)}) + "\n")
    except Exception:
        pass


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
        return False
    if not engine.passes_signal_gate(decision, knobs):
        return False

    open_positions = ex.fetch_all_positions()
    max_side = settings.max_same_side_positions
    if equity < settings.small_account_threshold:
        max_side = min(max_side, 2)
    ok_side, side_reason = same_side_exposure_ok(
        open_positions, decision.signal.value, max_same_side=max_side
    )
    if not ok_side:
        log.info("exposure gate %s: %s", symbol, side_reason)
        return False

    market = ex.market_for(symbol)
    if not market:
        return False
    if settings.scalp_3r_mode:
        cap = ex.symbol_leverage_cap(symbol)
        floor = min(40, int(settings.scalp_leverage_max))
        if cap < floor:
            log.debug("skip %s: exchange max %dx < %dx mission floor", symbol.split("/")[0], cap, floor)
            return False

    conf = getattr(decision, "model_confidence", 0.0) or (decision.score / 100.0)
    sp = engine.scalp
    sizer = MarginAwareSizer(
        free_margin=free_margin,
        fee_taker=d.fee_taker,
        fee_maker=d.fee_maker,
        min_take_profit_pct=sp.min_take_profit_pct if sp else d.min_take_profit_pct,
        base_leverage=sp.base_leverage if sp else d.base_leverage,
        max_leverage=(
            ex.symbol_leverage_cap(symbol)
            if settings.scalp_3r_mode
            else knobs.max_leverage
        ),
        margin_reserve_usdt=d.margin_reserve_usdt,
        risk_fraction=knobs.risk_per_trade_pct,
        model_confidence=conf,
        liquidation_buffer=d.liquidation_buffer_mult,
        scalp_mode=sp is not None,
        max_stop_pct=sp.max_stop_pct if sp else 0.08,
        max_take_pct=sp.max_take_pct if sp else 0.15,
        fee_coverage_multiple=sp.fee_coverage_multiple if sp else 2.0,
        margin_use_fraction=sp.margin_use_fraction if sp else 0.88,
        min_margin_rate=settings.min_margin_rate,
        min_rr=sp.min_rr if sp else 1.35,
    )
    plan = sizer.plan_trade(
        decision.close,
        decision.stop_pct,
        decision.take_pct,
        market.contract_size,
        market.min_size,
        margin_fraction=margin_fraction,
    )
    if plan is None:
        return False
    if plan.margin_usd > free_margin - d.min_free_margin_usdt:
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
        if "102135" in err or "market is closed" in err.lower():
            cooldowns.block(symbol, seconds=6 * 3600)
            log.warning("market closed %s — blocked 6h", symbol)
        return False
    if settings.dry_run:
        log.info("DRY_RUN open %s %s (no exchange order)", symbol, decision.signal.value)

    entry_px = decision.close
    if ex.stream:
        live = ex.stream.get_last_price(symbol)
        if live and live > 0:
            entry_px = live
    registry.record_open(
        symbol,
        side=decision.signal.value,
        entry_price=entry_px,
        leverage=plan.leverage,
        stop_pct=plan.stop_pct,
        take_pct=plan.take_pct,
        conviction=conviction,
    )
    repaired = getattr(ex, "last_repaired_tpsl", None)
    if repaired:
        rep_stop, rep_take = repaired
        registry.update_tpsl(symbol, stop_pct=rep_stop, take_pct=rep_take)
        rr = rep_take / max(rep_stop, 1e-9)
        log.info(
            "3R TPSL live %s stop=%.2f%% take=%.2f%% rr=%.2f:1 (exchange liq)",
            symbol,
            rep_stop * 100,
            rep_take * 100,
            rr,
        )
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
                tracker.record_entry(symbol, decision.signal.value, ep, sp, tp, feats.tolist(), decision.score)
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


def run_once(
    ex: BlofinExchange,
    settings: Settings,
    engine: AutonomousGrowthEngine,
    journal: TradeJournal,
    cooldowns: SymbolCooldowns,
    ml: MLPredictor,
    tracker: TradeOutcomeTracker | None,
    pacer: EntryPacer,
    registry: PositionRegistry,
    side_guard: SymbolSideGuard,
    steward: PositionSteward | None = None,
    optimizer: ScalpOptimizer | None = None,
) -> int:
    equity = ex.fetch_equity_usdt()
    free_margin = ex.fetch_free_equity_usdt()
    engine.snapshot_equity(equity)
    _snapshot_equity(settings.state_dir, equity)

    wr, pf, loss_streak = _load_performance_stats(settings.state_dir)
    engine.record_performance(wr, pf, loss_streak)
    _sync_ml_metrics(engine, ml, tracker)

    if settings.trade_all_symbols:
        lev = engine.scalp.base_leverage if engine.scalp else engine.doctrine.base_leverage
        markets = load_tradeable_markets(ex, equity, lev, 0.95, 9999)
        ex.refresh_markets(markets)
        symbols = [m.symbol for m in markets]
    else:
        symbols = [settings.symbol] if ex.market_for(settings.symbol) else []

    if steward:
        open_positions = steward.run_once_now()
        free_margin = ex.fetch_free_equity_usdt()
    else:
        open_positions = ex.fetch_all_positions()
        registry.sync_with_exchange(set(open_positions.keys()))
    engine.update_fluid(equity, free_margin, len(open_positions))
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
        "pnl=%s vert=%.0f%% harvest=%.2fx | conf>=%.0f%% need=%.2f%%/day",
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

    if optimizer and settings.optimizer_enabled:
        gap = effective_entry_gap(settings, optimizer.tuning)
    else:
        gap = knobs.min_seconds_between_entries
    if tp:
        gap = min(gap, tp.target_entry_gap)
    pacer.set_min_seconds(gap, floor=8.0 if settings.throughput_brain_enabled else 15.0)
    wait = pacer.seconds_until_ready()
    if not pacer.can_enter():
        log.debug("entry pacer: %.0fs until next highest-conviction slot", wait)
        return knobs.poll_seconds

    held = set(open_positions.keys())
    scan = _scan_symbols_ws(ex, symbols, held, knobs, open_count=len(open_positions))

    candidates = []
    for sym in scan:
        try:
            d = analyze_symbol(
                ex,
                settings,
                sym,
                ml,
                equity=equity,
                min_confidence=knobs.min_confidence,
                min_signal_score=knobs.min_signal_score,
            )
            if d and d.signal != Signal.FLAT and engine.passes_signal_gate(d, knobs):
                candidates.append((sym, d))
            time.sleep(0.05)
        except Exception:
            log.exception("scan %s", sym)

    ranked = rank_setups(
        candidates,
        knobs.path_reliability,
        mission_scale=engine.mission_scale_conviction,
    )
    max_open = effective_max_open(settings, equity)
    if max_open >= UNLIMITED_POSITIONS:
        per_tick = knobs.max_opens_per_tick
    else:
        open_slots = max(0, max_open - len(open_positions))
        per_tick = min(knobs.max_opens_per_tick, open_slots) if open_slots else 0
        if per_tick <= 0:
            return knobs.poll_seconds
    tp = engine._last_throughput
    starved = tp.starved if tp else (
        optimizer is not None
        and optimizer.tuning.trades_last_hour < settings.optimizer_target_min_tph
    )
    mission_floor = knobs.min_confidence
    if engine._last_mission is not None and not settings.unrestricted_trading:
        mission_floor = max(mission_floor, engine._last_mission.min_conviction)
    allow_apex_fallback = (
        (tp.allow_elite_fallback if tp else starved) or not settings.winner_apex_preferred
    )
    elite = select_conviction_ties(
        ranked,
        max_opens=per_tick,
        min_conviction=mission_floor,
        apex_preferred=settings.winner_apex_preferred,
        elite_only=settings.winner_elite_only,
        allow_elite_fallback=allow_apex_fallback,
    )
    if not elite:
        if ranked:
            top = ranked[0]
            want = "apex" if settings.winner_apex_preferred and not starved else "elite+"
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

    cd_sec = int(effective_cooldown_minutes(settings, optimizer.tuning if optimizer else None) * 60)
    if optimizer:
        cooldowns.cooldown_seconds = cd_sec

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
        )
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
                registry,
                side_guard,
                conviction=setup.conviction,
                margin_fraction=margin_frac,
                cooldown_seconds=cd_sec,
            ):
                opened += 1
                cooldowns.block(setup.symbol, seconds=cd_sec)
                open_positions[setup.symbol] = {"side": setup.decision.signal.value}
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

    if opened:
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
    if settings.unrestricted_trading:
        log.warning(
            "UNRESTRICTED_TRADING=true — drawdown/mission/fluid entry pauses DISABLED (SL/TP still on)"
        )
    log.info("AUTONOMOUS ENGINE: %s", engine.doctrine_summary())

    ex = BlofinExchange(settings)
    ex.load()
    eq0 = ex.fetch_equity_usdt()
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
        apex_note = (
            f"apex>={settings.winner_apex_score:.2f} preferred"
            if settings.winner_apex_preferred
            else "elite+"
        )
        log.warning(
            "50x 3R THROUGHPUT: %s | elite>=%.2f | target %d–%d opens/hr | lev=%d–%dx | "
            "max_open=%s | core_brain=on | hold/SL/TP unchanged",
            apex_note,
            settings.winner_elite_score,
            settings.optimizer_target_min_tph,
            settings.optimizer_target_max_tph,
            settings.scalp_leverage,
            settings.scalp_leverage_max,
            cap_msg,
        )

    if settings.trade_all_symbols:
        lev = engine.scalp.base_leverage if engine.scalp else engine.doctrine.base_leverage
        mkts = load_tradeable_markets(ex, ex.fetch_equity_usdt(), lev, 0.95, 9999)
        ex.refresh_markets(mkts)
        inst_ids = [m.inst_id for m in mkts]
    else:
        inst_ids = [symbol_to_inst_id(settings.symbol)]

    stream = BlofinMarketStream(ex.http, demo=settings.mode == "demo")
    stream.start(inst_ids)
    ex.attach_stream(stream)
    log.info("websocket + REST ticker hub | %d assets live", len(inst_ids))

    journal = TradeJournal(settings.state_dir / "trades.jsonl")
    registry = PositionRegistry(settings.state_dir)

    if settings.leverage_auto_upgrade:
        reconcile_open_book(ex, settings, registry, engine, max_closes_per_pass=2)

    cd_min = (
        settings.scalp_cooldown_minutes if settings.scalp_mode else engine.doctrine.symbol_cooldown_minutes
    )
    cooldowns = SymbolCooldowns(settings.state_dir / "cooldowns.json", cd_min * 60)
    side_guard = SymbolSideGuard(
        settings.state_dir,
        block_seconds=settings.symbol_flip_block_minutes * 60.0,
    )
    pacer = EntryPacer(settings.state_dir, engine.doctrine.min_entry_gap_seconds)
    ml = MLPredictor(
        settings.state_dir,
        min_confidence=engine.doctrine.min_confidence_floor,
        min_score=engine.doctrine.min_signal_score_default,
    )
    tracker = TradeOutcomeTracker(settings.state_dir, max_samples=settings.ml_real_feedback_max_samples)

    def _harvest_eagerness() -> float:
        c = engine.curve_state
        return c.harvest_eagerness if c else 1.0

    steward = PositionSteward(
        ex, settings, engine, registry, tracker, harvest_eagerness_fn=_harvest_eagerness
    )
    steward.start()

    ml_trainer: ContinuousMlTrainer | None = None
    if settings.signal_mode == "ml" and settings.ml_continuous_train:

        def _on_ml_updated() -> None:
            ml.reload()
            _sync_ml_metrics(engine, ml, tracker)
            engine.mark_retrained()
            if ml.is_ready():
                log.info("ML model live — %s", ml.metrics_summary())

        ml_trainer = ContinuousMlTrainer(ex, settings, on_model_updated=_on_ml_updated)
        ml_trainer.start()

    healer = SelfHealer(settings.state_dir, enabled=settings.self_heal_enabled)
    optimizer = ScalpOptimizer(settings.state_dir, settings)
    log.warning(
        "STARTUP: 3R scalp + winner-only + 15m optimizer (all in-process, single process)"
    )
    if settings.optimizer_enabled:
        log.warning(
            "15m OPTIMIZER ON | target %d–%d trades/hr | interval=%ds | no pause rails",
            settings.optimizer_target_min_tph,
            settings.optimizer_target_max_tph,
            int(settings.optimizer_interval_seconds),
        )
    if settings.signal_mode == "ml" and not ml.is_ready():
        if ml_trainer:
            log.info(
                "ML warming up — background trainer bootstrapping %d symbols (bot trades on confluence until deploy)",
                settings.ml_bootstrap_symbols,
            )
            ml_trainer.request_full_refit()
            healer.mark_refit_requested()
        else:
            log.info("ML not deployed — set ML_CONTINUOUS_TRAIN=true or run train_model.py")

    reloader = create_reloader(settings)
    runtime = RuntimeCtx(
        settings=settings,
        engine=engine,
        steward=steward,
        ml_trainer=ml_trainer,
        optimizer=optimizer,
        ml=ml,
        healer=healer,
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
            eq = ex.fetch_equity_usdt()
            fm = ex.fetch_free_equity_usdt()
            wr, pf, streak = _load_performance_stats(settings.state_dir)
            engine.record_performance(wr, pf, streak)
            open_n = len(ex.fetch_all_positions())
            _sync_ml_metrics(engine, ml, tracker)
            engine.update_fluid(eq, fm, open_n)
            knobs = engine.compute_knobs(eq, fm, open_n)
            healer.tick(engine, settings, eq, fm, knobs, ml, ml_trainer)
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
                    )
            if ml_trainer and tracker:
                ml_trainer.maybe_refit_from_outcomes(tracker)
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
                pacer,
                registry,
                side_guard,
                steward,
                optimizer,
            )
        except Exception:
            log.exception("tick failed")
        time.sleep(poll)


if __name__ == "__main__":
    main()
