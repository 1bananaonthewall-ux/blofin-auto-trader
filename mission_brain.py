"""
Mission brain — one purpose only.

The engine does not generalize, socialize, or optimize for anything except:
maintain and exceed 10% account growth per day — nothing else.

Every tick, trade, harvest, scan depth, and risk knob is filtered through that lens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from growth_optimizer import GrowthMetrics
from pnl_curve import PnlCurveState
from mission_config import (
    TARGET_DAILY_GROWTH_PCT,
    daily_growth_shortfall_pct,
    progress_toward_daily_goal_pct,
    sole_objective_label,
)

log = logging.getLogger(__name__)

SOLE_OBJECTIVE = sole_objective_label()


@dataclass(frozen=True)
class MissionState:
    sole_objective: str
    progress_pct: float
    schedule_pressure: float
    mission_focus: float
    behind_schedule: bool
    directive: str
    min_conviction: float
    risk_multiplier: float
    entry_allowed: bool


class MissionBrain:
    """Single-minded intelligence: only the +10%/day mission exists."""

    def __init__(self) -> None:
        self._last: MissionState | None = None

    @property
    def last_state(self) -> MissionState | None:
        return self._last

    def evaluate(
        self,
        equity: float,
        metrics: GrowthMetrics,
        curve: PnlCurveState | None,
        *,
        path_reliability: float = 0.5,
        survival: float = 0.5,
        account_curve_maximize: bool = False,
    ) -> MissionState:
        day_start = 0.0
        if metrics.projected_capital_at_target > 0 and equity > 0:
            day_start = metrics.projected_capital_at_target / (1 + TARGET_DAILY_GROWTH_PCT / 100)
        actual_today = (equity / day_start - 1.0) * 100.0 if day_start > 0 and equity > 0 else 0.0

        progress = progress_toward_daily_goal_pct(actual_today)
        behind = not metrics.on_track

        shortfall = daily_growth_shortfall_pct(actual_today)
        schedule_pressure = _clamp01(shortfall / max(TARGET_DAILY_GROWTH_PCT, 1e-9))
        if behind:
            schedule_pressure = max(schedule_pressure, 0.55)

        curve_vert = curve.verticality if curve else 0.5
        curve_phase = curve.curve_phase if curve else "flat"
        preserve = curve.preserve_capital if curve else False

        curve_weight = 0.38 if account_curve_maximize else 0.25
        mission_focus = _clamp01(
            0.32 * min(1.2, progress / 100.0)
            + curve_weight * curve_vert
            + 0.18 * path_reliability
            + 0.12 * (1.0 - schedule_pressure * 0.5)
        )
        if curve and account_curve_maximize:
            slope_push = _clamp01((curve.slope_1h_pct + 2.0) / max(TARGET_DAILY_GROWTH_PCT, 1.0))
            mission_focus = _clamp01(mission_focus * 0.65 + 0.35 * slope_push)
        if curve_phase == "declining" and not account_curve_maximize:
            mission_focus *= 0.55
        elif curve_phase == "declining" and account_curve_maximize:
            mission_focus *= 0.82
        elif curve_phase == "vertical":
            mission_focus = min(1.0, mission_focus * 1.18)
        elif curve_phase == "flat" and account_curve_maximize:
            mission_focus = min(1.0, mission_focus * 1.08)

        if account_curve_maximize and behind:
            risk_mult = min(1.42, 1.05 + schedule_pressure * 0.35 + curve_vert * 0.15)
            min_conv = 0.50 if curve_phase in ("vertical", "climbing", "flat") else 0.56
            directive = "ACCOUNT CURVE — steepen balance; high-conviction entries until +10%/day"
            entry_ok = survival >= 0.12 and path_reliability >= 0.12
        elif behind and curve_phase in ("vertical", "climbing"):
            risk_mult = min(1.25, 1.0 + schedule_pressure * 0.2)
            min_conv = 0.54
            directive = "BELOW +10% — press high-conviction trades to maintain/exceed daily goal"
            # Short bounce on a red day is not permission to stack risk (micro accounts only).
            if equity < 50 and actual_today < -2.0 and curve_vert < 0.55:
                min_conv = 0.68
                directive = "RED DAY — need strong edge despite short-term climb"
                entry_ok = survival >= 0.35 and path_reliability >= 0.3
            else:
                entry_ok = survival >= 0.2
        elif behind:
            risk_mult = max(0.45, 0.85 - schedule_pressure * 0.35)
            min_conv = 0.68
            directive = "BELOW +10% — protect base; elite entries only toward 10%+ day"
            entry_ok = survival >= 0.35 and path_reliability >= 0.25
        elif preserve and not account_curve_maximize:
            risk_mult = 0.5
            min_conv = 0.70
            directive = "PRESERVE COMPOUNDING BASE — vertical curve required before size"
            entry_ok = path_reliability >= 0.4 and curve_vert >= 0.5
        elif preserve and account_curve_maximize:
            risk_mult = 0.75
            min_conv = 0.62
            directive = "ACCOUNT CURVE DIP — selective entries; rebuild slope"
            entry_ok = survival >= 0.2 and path_reliability >= 0.2
        elif actual_today >= TARGET_DAILY_GROWTH_PCT:
            risk_mult = min(1.15, 0.92 + mission_focus * 0.2)
            min_conv = 0.50
            directive = "EXCEEDING +10% — keep compounding above daily floor"
            entry_ok = True
        else:
            risk_mult = min(1.15, 0.9 + mission_focus * 0.25)
            min_conv = 0.52
            directive = "ON MISSION — maintain and exceed +10% account growth today"
            entry_ok = True

        if equity < 50 and behind:
            risk_mult = min(1.35, risk_mult * 1.1)
            min_conv = min(min_conv, 0.58)

        state = MissionState(
            sole_objective=SOLE_OBJECTIVE,
            progress_pct=round(progress, 4),
            schedule_pressure=round(schedule_pressure, 4),
            mission_focus=round(mission_focus, 4),
            behind_schedule=behind,
            directive=directive,
            min_conviction=round(min_conv, 3),
            risk_multiplier=round(risk_mult, 3),
            entry_allowed=entry_ok,
        )
        self._last = state
        return state

    def permits_trade(self, conviction: float, state: MissionState | None = None) -> tuple[bool, str]:
        """Would opening this setup advance the only objective?"""
        st = state or self._last
        if st is None:
            return False, "mission state unknown"
        if not st.entry_allowed:
            return False, st.directive
        if conviction < st.min_conviction:
            return (
                False,
                f"conviction {conviction:.3f} below mission floor {st.min_conviction:.3f} for +10%/day",
            )
        if st.behind_schedule and conviction < 0.62 and st.schedule_pressure > 0.7:
            return False, "below +10% daily mission — need stronger edge"
        return True, "advances sole objective"

    def scale_conviction(self, raw_conviction: float, state: MissionState | None = None) -> float:
        """Reweight setup rank by mission relevance (not chat — decision math)."""
        st = state or self._last
        if st is None:
            return raw_conviction
        scaled = raw_conviction * (0.7 + 0.3 * st.mission_focus)
        if st.behind_schedule and raw_conviction >= st.min_conviction + 0.08:
            scaled = min(1.0, scaled * 1.06)
        if st.schedule_pressure > 0.75 and raw_conviction < st.min_conviction:
            scaled *= 0.5
        return round(scaled, 4)

    def format_focus(self, equity: float, metrics: GrowthMetrics, state: MissionState) -> str:
        return (
            f"MISSION BRAIN | sole purpose: {state.sole_objective}\n"
            f"  today_progress={state.progress_pct:.1f}% of +{TARGET_DAILY_GROWTH_PCT:.0f}% goal | "
            f"equity=${equity:,.4f} | EOD target=${metrics.projected_capital_at_target:,.4f}\n"
            f"  focus={state.mission_focus:.0%} schedule_pressure={state.schedule_pressure:.0%}\n"
            f"  >> {state.directive}"
        )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
