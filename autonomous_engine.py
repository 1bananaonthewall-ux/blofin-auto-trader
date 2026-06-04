"""
Autonomous Growth Engine — fluid control toward mission_config sole objective.

No discrete modes. A continuous manifold of signals blends each tick into
how hard to trade, how selective to be, and whether new entries are reliable.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fluid_manifold import FluidManifold, FluidSnapshot, ManifoldContext
from growth_optimizer import CompoundGrowthOptimizer
from core_brain import CoreBrain, CoreDirective
from markov_regime import MarkovSnapshot, get_markov_engine
from mission_brain import MissionBrain, MissionState
from pnl_curve import PnlCurveEngine, PnlCurveState
from scalp_profile import ScalpProfile, profile_for
from throughput_brain import ThroughputState

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

from mission_config import TARGET_DAILY_GROWTH_PCT, sole_objective_label


@dataclass(frozen=True)
class TradingDoctrine:
    target_daily_growth_pct: float = TARGET_DAILY_GROWTH_PCT
    sole_objective: str = sole_objective_label()
    signal_mode: str = "ml"
    min_confidence_floor: float = 0.68
    min_confidence_default: float = 0.74
    min_confidence_strict: float = 0.82
    min_signal_score_default: float = 65.0
    never_close_on_signal_flip: bool = True
    always_attach_sltp_at_entry: bool = True
    maintain_sltp_on_open_positions: bool = True
    continuous_position_steward: bool = True
    unlimited_positions_margin_gated: bool = True
    max_leverage_cap: int = 25
    base_leverage: int = 10
    liquidation_buffer_mult: float = 1.35
    margin_reserve_usdt: float = 0.05
    min_free_margin_usdt: float = 0.01
    fee_taker: float = 0.0006
    fee_maker: float = 0.0002
    min_take_profit_pct: float = 0.004
    base_risk_per_trade_pct: float = 0.10
    symbols_per_tick_base: int = 120
    poll_seconds_base: int = 25
    ml_retrain_hours: int = 12
    symbol_cooldown_minutes: int = 20
    min_entry_gap_seconds: float = 75.0
    margin_deploy_base_pct: float = 0.08
    margin_deploy_max_pct: float = 0.28
    max_opens_per_tick: int = 3
    conviction_tie_abs_gap: float = 0.022
    conviction_tie_rel_gap: float = 0.035


@dataclass
class RuntimeKnobs:
    min_confidence: float
    min_signal_score: float
    risk_per_trade_pct: float
    max_leverage: int
    symbols_per_tick: int
    poll_seconds: int
    aggression_boost: float
    required_daily_return_pct: float
    on_track: bool
    days_remaining: int
    should_retrain_ml: bool
    action_intensity: float
    path_reliability: float
    survival: float
    edge: float
    allow_new_entries: bool
    drawdown_pct: float
    growth_pressure: float
    drivers: list[str]
    min_seconds_between_entries: float
    margin_deploy_base_pct: float
    margin_deploy_max_pct: float
    max_opens_per_tick: int
    harvest_eagerness: float
    curve_verticality: float
    curve_phase: str
    preserve_capital: bool
    mission_focus: float
    mission_directive: str


class AutonomousGrowthEngine:
    doctrine = TradingDoctrine()

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.growth = CompoundGrowthOptimizer(state_dir)
        self.manifold = FluidManifold(state_dir)
        self.pnl = PnlCurveEngine(state_dir)
        self.mission = MissionBrain()
        self.core = CoreBrain()
        self._last_fluid: FluidSnapshot | None = None
        self._last_curve: PnlCurveState | None = None
        self._last_mission: MissionState | None = None
        self._last_core: CoreDirective | None = None
        self._last_throughput: ThroughputState | None = None
        self._last_markov: MarkovSnapshot | None = None
        self._last_retrain_ts = 0.0
        self.unrestricted_trading = False
        self.recovery_active = False
        self.recovery_until = 0.0
        self._last_report_hour = -1
        self._recent_win_rate = 0.5
        self._profit_factor = 1.0
        self._consecutive_losses = 0
        self._ml_metrics: dict[str, float] = {}
        self.scalp: ScalpProfile | None = None
        self.settings: Settings | None = None
        self._tpsl_pacer = None
        self._exit_cooldowns = None

    def bind_settings(self, settings: "Settings") -> None:
        self.settings = settings
        self.scalp = profile_for(settings)
        if self.scalp:
            self.unrestricted_trading = settings.unrestricted_trading

    def _account_curve_maximize(self) -> bool:
        st = self.settings
        return bool(
            st
            and getattr(st, "account_curve_maximize", True)
            and not self.unrestricted_trading
        )

    def set_ml_metrics(
        self,
        val_accuracy: float,
        long_precision: float,
        short_precision: float,
        feedback_samples: int,
    ) -> None:
        self._ml_metrics = {
            "val_accuracy": val_accuracy,
            "long_precision": long_precision,
            "short_precision": short_precision,
            "feedback_samples": float(feedback_samples),
        }

    def record_performance(
        self, win_rate: float, profit_factor: float, consecutive_losses: int = 0
    ) -> None:
        self._recent_win_rate = max(0.0, min(1.0, win_rate))
        self._profit_factor = max(0.1, min(5.0, profit_factor))
        self._consecutive_losses = max(0, consecutive_losses)

    def snapshot_equity(self, equity: float) -> None:
        self.growth.record_equity_snapshot(equity)

    def update_curve(self, equity: float) -> PnlCurveState:
        metrics = self.growth.get_growth_metrics(equity)
        opens_starved = False
        st = self.settings
        if st is not None:
            from hourly_3r import hourly_3r_active, is_opens_starved
            from scalp_optimizer import get_active_tuning

            if hourly_3r_active(st):
                opens_starved = is_opens_starved(st, get_active_tuning())
        self._last_curve = self.pnl.update(
            equity,
            metrics.required_daily_return_pct,
            unrestricted=self.unrestricted_trading,
            account_curve_maximize=self._account_curve_maximize(),
            opens_starved=opens_starved,
        )
        return self._last_curve

    def bind_tpsl_pacer(self, pacer) -> None:
        self._tpsl_pacer = pacer

    def bind_exit_cooldowns(self, cooldowns) -> None:
        self._exit_cooldowns = cooldowns

    def record_closed_trade(
        self,
        symbol: str,
        net_pnl_usd: float,
        *,
        side: str = "",
        event: str = "close",
        roe_pct: float | None = None,
        entry: float | None = None,
        exit_px: float | None = None,
        leverage: int | None = None,
        margin_usdt: float | None = None,
        contracts: float | None = None,
    ) -> None:
        from roe_learning import get_roe_store, journal_open_before, resolve_close_pnl_roe

        margin = float(margin_usdt or 0)
        contracts_v = float(contracts or 0) or None
        lev = leverage
        if margin <= 0 or not contracts_v:
            j_margin, j_contracts, j_lev = journal_open_before(
                self.pnl.state_dir, symbol, time.time()
            )
            if margin <= 0 and j_margin > 0:
                margin = j_margin
            if not contracts_v and j_contracts > 0:
                contracts_v = j_contracts
            if not lev and j_lev > 0:
                lev = j_lev
        if roe_pct is None and entry and exit_px and margin > 0:
            _, roe_pct = resolve_close_pnl_roe(
                side=side or "long",
                entry=float(entry),
                exit_px=float(exit_px),
                prof_pnl=float(net_pnl_usd),
                margin_usdt=margin,
                leverage=lev,
                contracts=contracts_v,
            )
        elif roe_pct is None and margin > 0:
            roe_pct = round(float(net_pnl_usd) / margin * 100.0, 2)
        self.pnl.record_trade(
            symbol,
            net_pnl_usd,
            side=side,
            event=event,
            roe_pct=roe_pct,
            entry=entry,
            exit_px=exit_px,
            leverage=leverage,
        )
        get_roe_store(self.pnl.state_dir).record_close(
            symbol,
            side=side or "long",
            roe_pct=roe_pct,
            pnl_usd=net_pnl_usd,
            event=event,
            entry=entry,
            exit_px=exit_px,
            leverage=leverage,
            margin_usdt=margin_usdt,
            contracts=contracts,
        )
        if self._tpsl_pacer is not None:
            kind = self._tpsl_pacer.record_exit(event, net_pnl_usd)
            if kind == "sl" and self._exit_cooldowns is not None:
                sec = int(self._tpsl_pacer.symbol_sl_cooldown)
                if sec > 0:
                    self._exit_cooldowns.block(symbol, seconds=sec)

    def update_fluid(
        self, equity: float, free_margin: float, open_count: int
    ) -> FluidSnapshot:
        metrics = self.growth.get_growth_metrics(equity)
        curve = self.update_curve(equity)
        slope_norm = _clamp01((curve.slope_1h_pct / max(metrics.required_daily_return_pct, 0.5)) / 2.0)
        accel_norm = _clamp01(0.5 + curve.acceleration / 6.0)
        ctx = ManifoldContext(
            equity=equity,
            free_margin=free_margin,
            open_count=open_count,
            win_rate=self._recent_win_rate,
            profit_factor=self._profit_factor,
            consecutive_losses=self._consecutive_losses,
            required_daily_pct=metrics.required_daily_return_pct,
            on_track=metrics.on_track,
            days_remaining=metrics.days_remaining,
            aggression_boost=metrics.aggression_boost,
            ml_val_accuracy=self._ml_metrics.get("val_accuracy", 0.55),
            ml_long_precision=self._ml_metrics.get("long_precision", 0.5),
            ml_short_precision=self._ml_metrics.get("short_precision", 0.5),
            feedback_samples=int(self._ml_metrics.get("feedback_samples", 0)),
            pnl_verticality=curve.verticality,
            curve_slope_norm=slope_norm,
            curve_acceleration_norm=accel_norm,
        )
        self._last_fluid = self.manifold.tick(
            ctx,
            unrestricted=self.unrestricted_trading,
            account_curve_maximize=self._account_curve_maximize(),
        )
        metrics = self.growth.get_growth_metrics(equity)
        self._last_mission = self.mission.evaluate(
            equity,
            metrics,
            curve,
            path_reliability=self._last_fluid.path_reliability,
            survival=self._last_fluid.survival,
            account_curve_maximize=self._account_curve_maximize(),
        )
        return self._last_fluid

    @property
    def curve_state(self) -> PnlCurveState | None:
        return self._last_curve

    @property
    def mission_state(self) -> MissionState | None:
        return self._last_mission

    @property
    def core_directive(self) -> CoreDirective | None:
        return self._last_core or self.core.last

    def update_markov_global(self, ohlcv_1m: list[list[float]], state_dir: Path | None = None) -> None:
        if not ohlcv_1m:
            return
        eng = get_markov_engine(state_dir)
        snap = eng.update("global", ohlcv_1m)
        if snap:
            self._last_markov = snap

    @property
    def markov_state(self) -> MarkovSnapshot | None:
        return self._last_markov

    def evaluate_core(
        self,
        settings: "Settings",
        *,
        equity: float,
        free_margin: float,
        opens_last_hour: int,
        open_count: int,
        low_leverage_positions: int = 0,
    ) -> CoreDirective:
        """Single tick: mission + throughput + book policy."""
        metrics = self.growth.get_growth_metrics(equity)
        curve = self._last_curve or self.update_curve(equity)
        fluid = self._last_fluid
        if fluid is None:
            fluid = self.update_fluid(equity, free_margin, open_count)
        markov = self._last_markov if settings.markov_regime_enabled else None
        directive = self.core.evaluate(
            settings,
            equity=equity,
            free_margin=free_margin,
            metrics=metrics,
            curve=curve,
            fluid=fluid,
            opens_last_hour=opens_last_hour,
            open_count=open_count,
            low_leverage_positions=low_leverage_positions,
            unrestricted=self.unrestricted_trading,
            markov=markov,
        )
        self._last_core = directive
        self._last_throughput = _core_to_throughput(directive)
        return directive

    def fluid_wants_retrain(self) -> bool:
        return self.manifold.consume_retrain_flag()

    def activate_recovery(self, duration_sec: float) -> None:
        until = time.time() + max(60.0, duration_sec)
        self.recovery_active = True
        self.recovery_until = max(self.recovery_until, until)

    def _recovery_live(self) -> bool:
        if not self.recovery_active:
            return False
        if time.time() > self.recovery_until:
            self.recovery_active = False
            return False
        return True

    def compute_knobs(self, equity: float, free_margin: float, open_count: int) -> RuntimeKnobs:
        fluid = self._last_fluid or self.update_fluid(equity, free_margin, open_count)
        curve = self._last_curve or self.update_curve(equity)
        mission = self._last_mission
        if mission is None:
            metrics = self.growth.get_growth_metrics(equity)
            mission = self.mission.evaluate(
                equity,
                metrics,
                curve,
                path_reliability=fluid.path_reliability,
                survival=fluid.survival,
                account_curve_maximize=self._account_curve_maximize(),
            )
            self._last_mission = mission
        metrics = self.growth.get_growth_metrics(equity)
        acm = self._account_curve_maximize()
        d = self.doctrine
        sp = self.scalp
        ai = fluid.action_intensity * curve.entry_scale
        rel = fluid.path_reliability * (0.55 + 0.45 * curve.verticality)
        surv = fluid.survival * curve.risk_scale

        recovery = self._recovery_live()

        if self.unrestricted_trading:
            ai = max(ai, 0.45)
            rel = max(rel, 0.45)
            min_conf = d.min_confidence_floor
            min_score = d.min_signal_score_default
            risk_pct = max(
                d.base_risk_per_trade_pct * 0.65,
                d.base_risk_per_trade_pct * ai * rel * metrics.aggression_boost,
            )
        elif recovery:
            ai = max(ai, 0.38)
            rel = max(rel, 0.38)
            min_conf = _blend(d.min_confidence_floor, d.min_confidence_default, 0.55)
            min_score = _blend(58.0, d.min_signal_score_default, 0.5)
            risk_pct = max(
                d.base_risk_per_trade_pct * 0.5,
                d.base_risk_per_trade_pct * ai * rel * metrics.aggression_boost * 0.85,
            )
        else:
            min_conf = _blend(
                d.min_confidence_floor,
                d.min_confidence_strict,
                1.0 - rel * 0.85,
            )
            min_conf = _blend(min_conf, d.min_confidence_default, ai * 0.5)
            if curve.preserve_capital and not acm:
                min_conf = _blend(min_conf, d.min_confidence_strict, 0.65)
            if not curve.on_vertical_path and not acm:
                min_conf = _blend(min_conf, d.min_confidence_strict, 0.35)
            elif acm and curve.curve_phase in ("flat", "climbing"):
                min_conf = min(min_conf, mission.min_conviction + 0.02)
            min_conf = max(min_conf, mission.min_conviction)
            min_score = _blend(52.0, 72.0, 1.0 - rel * 0.7)
            min_score = _blend(min_score, d.min_signal_score_default, ai * 0.4)
            risk_pct = (
                d.base_risk_per_trade_pct
                * ai
                * rel
                * metrics.aggression_boost
                * curve.risk_scale
                * mission.risk_multiplier
            )
        risk_pct = max(0.0, min(0.18, risk_pct))
        if acm:
            risk_pct = min(0.18, risk_pct * (0.92 + 0.12 * curve.verticality))
            if curve.curve_phase in ("vertical", "climbing", "flat"):
                deploy_boost = 1.0 + 0.12 * curve.verticality
            else:
                deploy_boost = 1.0
        else:
            deploy_boost = 1.0
        if equity < 50:
            risk_pct = min(0.16, risk_pct * 1.15)

        base_lev = sp.base_leverage if sp else d.base_leverage
        lev_cap = sp.max_leverage_cap if sp else d.max_leverage_cap
        stg = self.settings
        if sp and sp.three_r_mode and stg and stg.throughput_brain_enabled:
            max_lev = lev_cap
        else:
            max_lev = int(base_lev + (lev_cap - base_lev) * ai * rel)
            max_lev = max(base_lev, min(lev_cap, max_lev))

        symbols_tick = max(30, int(d.symbols_per_tick_base * (0.3 + 0.7 * ai * rel)))
        poll_base = sp.poll_seconds_base if sp else d.poll_seconds_base
        poll = int(_blend(55, poll_base, ai))
        poll_lo, poll_hi = (10, 45) if sp else (15, 90)
        poll = max(poll_lo, min(poll_hi, poll))

        should_retrain = (time.time() - self._last_retrain_ts) > d.ml_retrain_hours * 3600
        if not self.unrestricted_trading and (fluid.force_retrain or surv < 0.25):
            should_retrain = True

        # One best conviction per cycle; gap scales inversely with intensity (never machine-gun)
        gap_base = sp.min_entry_gap_seconds if sp else d.min_entry_gap_seconds
        entry_gap = _blend(120.0, gap_base, ai)
        if sp:
            entry_gap = min(entry_gap, gap_base * 1.15)
        st_pace = self.settings
        tpsl_pace_only = False
        if st_pace is not None:
            from tpsl_pacing import use_tpsl_only_pacing

            tpsl_pace_only = use_tpsl_only_pacing(st_pace)
        if tpsl_pace_only and st_pace is not None:
            entry_gap = float(getattr(st_pace, "tpsl_pace_base_gap_seconds", 2.0))
        else:
            core_d = self._last_core or self.core.last
            if core_d is not None:
                entry_gap = min(entry_gap, core_d.target_entry_gap)
                max_lev = max(max_lev, min(lev_cap, core_d.target_leverage))
            elif self._last_throughput is not None:
                tp_state = self._last_throughput
                entry_gap = min(entry_gap, tp_state.target_entry_gap)
                max_lev = max(max_lev, min(lev_cap, tp_state.target_leverage))
        if sp:
            deploy_base = sp.margin_deploy_base * rel * curve.entry_scale
            deploy_max = sp.margin_deploy_max * ai * rel * curve.entry_scale
        else:
            deploy_base = d.margin_deploy_base_pct * rel * curve.entry_scale
            deploy_max = d.margin_deploy_max_pct * ai * rel * curve.entry_scale

        st = self.settings
        if st and equity > 0 and equity < st.small_account_threshold:
            cap_max = 0.32 if acm else 0.28
            cap_base = 0.14 if acm else 0.12
            deploy_max = min(deploy_max, cap_max)
            deploy_base = min(deploy_base, cap_base)
        if acm:
            deploy_base = min(0.22, deploy_base * deploy_boost)
            deploy_max = min(0.38, deploy_max * deploy_boost)

        max_opens = d.max_opens_per_tick
        if st:
            from account_guard import effective_max_opens_per_tick

            max_opens = effective_max_opens_per_tick(st, equity, max_opens)

        if self.unrestricted_trading or recovery:
            allow_entries = equity > 0 and free_margin > d.min_free_margin_usdt and risk_pct > 0.001
        elif st_pace is not None and tpsl_pace_only:
            allow_entries = equity > 0 and free_margin > d.min_free_margin_usdt and risk_pct > 0.001
        else:
            allow_entries = (
                fluid.allow_new_entries
                and risk_pct > 0.001
                and mission.entry_allowed
            )
            if acm:
                if curve.curve_phase == "declining":
                    allow_entries = allow_entries and curve.drawdown_from_peak_pct < 14
                elif curve.preserve_capital:
                    allow_entries = allow_entries and curve.entry_scale >= 0.55
            elif curve.curve_phase == "declining":
                allow_entries = allow_entries and curve.verticality > 0.45 and ai > 0.25
            elif curve.preserve_capital:
                allow_entries = allow_entries and curve.entry_scale >= 0.4
            if (
                st
                and equity > 0
                and equity < st.small_account_threshold
                and open_count >= max(4, st.micro_equity_max_open + 1)
                and curve.curve_phase == "declining"
                and curve.actual_daily_pct < -2.0
            ):
                allow_entries = False
            if (
                st
                and equity < st.small_account_threshold
                and open_count >= 5
                and curve.actual_daily_pct < -3.0
                and curve.drawdown_from_peak_pct > 8.0
            ):
                allow_entries = False
            if st and equity >= st.small_account_threshold:
                deploy_base = max(deploy_base, 0.14)
                deploy_max = max(deploy_max, min(0.38, st.margin_use_fraction * 0.42))
                max_opens = max(max_opens, min(5, st.max_opens_per_tick))
                entry_gap = min(entry_gap, max(25.0, gap_base * 0.55))
                symbols_tick = max(symbols_tick, min(220, d.symbols_per_tick_base))
            if st and equity >= 100.0:
                deploy_base = max(deploy_base, 0.18)
                deploy_max = max(deploy_max, min(0.48, st.margin_use_fraction * 0.55))
                max_opens = max(max_opens, min(6, st.max_opens_per_tick))
                entry_gap = min(entry_gap, 20.0)
        # Hourly 3R winner mode: fast scan, enough opens for >=1 TP win/hour at 3:1.
        if st and getattr(st, "hourly_3r_winner_mode", False):
            from hourly_3r import target_wins_per_hour

            wins_tgt = target_wins_per_hour(st)
            min_conf = min(min_conf, 0.54 if wins_tgt >= 3 else 0.56)
            min_score = min(min_score, 48.0 if wins_tgt >= 3 else 50.0)
            symbols_tick = max(symbols_tick, min(240, st.symbols_per_tick))
            poll = max(5, min(poll, 8 if wins_tgt >= 3 else 10))
            entry_gap = min(entry_gap, 5.0 if wins_tgt >= 3 else 7.0)
            max_opens = max(max_opens, min(6, st.max_opens_per_tick))
            deploy_base = max(deploy_base, 0.14)
            deploy_max = max(deploy_max, 0.26)
        if st:
            from account_guard import universe_fill_active

            if universe_fill_active(st):
                min_conf = min(min_conf, 0.52)
                min_score = min(min_score, 48.0)
                symbols_tick = max(symbols_tick, min(280, st.symbols_per_tick))
                poll = max(5, min(poll, 8))
                entry_gap = min(entry_gap, 4.0)
                max_opens = max(max_opens, min(16, st.max_opens_per_tick))
                deploy_base = max(deploy_base, 0.12)
                deploy_max = max(deploy_max, min(0.32, st.margin_use_fraction * 0.45))
        # Momentum wave mode: bias toward concurrent momentum runners and faster recycle.
        elif st and getattr(st, "momentum_wave_mode", False):
            min_conf = min(min_conf, 0.56)
            min_score = min(min_score, 52.0)
            symbols_tick = max(symbols_tick, min(220, st.symbols_per_tick))
            poll = max(8, min(poll, 18))
            entry_gap = min(entry_gap, 10.0)
            max_opens = max(max_opens, min(8, st.max_opens_per_tick))
            deploy_base = max(deploy_base, 0.16)
            deploy_max = max(deploy_max, 0.30)

        return RuntimeKnobs(
            min_confidence=round(min_conf, 3),
            min_signal_score=round(min_score, 1),
            risk_per_trade_pct=round(risk_pct, 4),
            max_leverage=max_lev,
            symbols_per_tick=symbols_tick,
            poll_seconds=poll,
            aggression_boost=metrics.aggression_boost,
            required_daily_return_pct=metrics.required_daily_return_pct,
            on_track=metrics.on_track,
            days_remaining=metrics.days_remaining,
            should_retrain_ml=should_retrain,
            action_intensity=ai,
            path_reliability=rel,
            survival=surv,
            edge=fluid.edge,
            allow_new_entries=allow_entries,
            drawdown_pct=fluid.drawdown_pct,
            growth_pressure=fluid.growth_pressure,
            drivers=fluid.drivers,
            min_seconds_between_entries=round(entry_gap, 1),
            margin_deploy_base_pct=round(deploy_base, 4),
            margin_deploy_max_pct=round(min(0.35, deploy_max), 4),
            max_opens_per_tick=max_opens,
            harvest_eagerness=(
                (
                    min(0.55, curve.harvest_eagerness * 0.72)
                    if acm and curve.curve_phase in ("vertical", "climbing")
                    else min(0.78, curve.harvest_eagerness * 0.88)
                )
                if getattr(self.settings, "stack_winners_mode", True)
                else (
                    min(1.75, curve.harvest_eagerness * 1.25) if sp else curve.harvest_eagerness
                )
            ),
            curve_verticality=curve.verticality,
            curve_phase=curve.curve_phase,
            preserve_capital=curve.preserve_capital,
            mission_focus=mission.mission_focus,
            mission_directive=mission.directive,
        )

    def mark_retrained(self) -> None:
        self._last_retrain_ts = time.time()

    def maybe_log_report(self, equity: float) -> None:
        hour = int(time.time() // 3600)
        if hour != self._last_report_hour:
            self._last_report_hour = hour
            log.info("\n%s", self.growth.format_growth_report(equity))
            curve = self._last_curve
            if curve is not None:
                log.info("\n%s", self.pnl.format_report(curve, equity))
            curve = self._last_curve or self.update_curve(equity)
            metrics = self.growth.get_growth_metrics(equity)
            log.info("\n%s", self.pnl.format_report(curve, equity))
            if self.core.last:
                log.info("\n%s", self.core.format_status(equity, metrics))
            elif self._last_mission:
                log.info("\n%s", self.mission.format_focus(equity, metrics, self._last_mission))
            log.info(
                "manifold ~%s tuned dimensions | fluid field active",
                f"{self.manifold.parameter_count_estimate:,}",
            )

    def passes_signal_gate(
        self,
        decision,
        knobs: RuntimeKnobs,
        *,
        min_confidence: float | None = None,
        min_signal_score: float | None = None,
    ) -> bool:
        from strategy import Signal

        if decision is None or decision.signal == Signal.FLAT:
            return False
        conf = getattr(decision, "model_confidence", 0.0) or (decision.score / 100.0)
        min_c = knobs.min_confidence if min_confidence is None else min_confidence
        min_s = knobs.min_signal_score if min_signal_score is None else min_signal_score
        zone = getattr(decision, "confluence_zone", "") or ""
        if zone == "llm" and self.settings is not None:
            min_c = min(min_c, self.settings.llm_trading_min_confidence)
            min_s = min(min_s, self.settings.llm_trading_min_score)
        if conf < min_c:
            return False
        if decision.score < min_s:
            return False
        # Extra fluid gate: signal must clear path reliability bar
        margin = abs(conf - 0.5) * 2
        if margin * knobs.path_reliability < 0.08:
            return False
        conv = getattr(decision, "model_confidence", 0.0) or (decision.score / 100.0)
        cf = getattr(decision, "confluence_score", None) or conv
        if self.unrestricted_trading:
            return True
        if self.core.last:
            ok, _reason = self.core.permits_trade(float(cf))
            return ok
        ok, _reason = self.mission.permits_trade(float(cf), self._last_mission)
        return ok

    def doctrine_summary(self) -> str:
        d = self.doctrine
        scalp = ""
        if self.scalp:
            sp = self.scalp
            scalp = (
                f" | SCALP {sp.base_leverage}-{sp.max_leverage_cap}x "
                f"poll~{sp.poll_seconds_base}s harvest@{sp.min_hold_seconds:.0f}s"
            )
        return (
            f"CORE BRAIN: sole purpose {d.sole_objective} — "
            f"no other goals | fluid manifold + ML + confluence | "
            f"up to {d.max_opens_per_tick} opens/cycle only if tied at top | margin from live free"
            f"{scalp}"
        )

    def mission_scale_conviction(self, raw_conviction: float) -> float:
        if self.core.last:
            return self.core.scale_conviction(raw_conviction)
        return self.mission.scale_conviction(raw_conviction, self._last_mission)


def _core_to_throughput(d: CoreDirective) -> ThroughputState:
    return ThroughputState(
        opens_last_hour=d.opens_last_hour,
        starved=d.starved,
        overheating=d.overheating,
        target_entry_gap=d.target_entry_gap,
        target_leverage=d.target_leverage,
        allow_elite_fallback=d.allow_elite_fallback,
        should_rotate_leverage=False,
        should_repair_book=d.maintain_open_book,
        directive=d.summary,
    )


def _blend(lo: float, hi: float, t: float) -> float:
    t = max(0.0, min(1.0, t))
    return lo + (hi - lo) * t


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def create_engine(state_dir: Path) -> AutonomousGrowthEngine:
    return AutonomousGrowthEngine(state_dir)
