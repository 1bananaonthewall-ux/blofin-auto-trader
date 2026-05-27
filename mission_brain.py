"""
Mission brain — one purpose only.

The engine does not generalize, socialize, or optimize for anything except:
reach $95,000,000 by 2027-09-01 — nothing else.

Every tick, trade, harvest, scan depth, and risk knob is filtered through that lens.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from growth_optimizer import GrowthMetrics
from mission_config import (
    TARGET_CAPITAL_USD,
    TARGET_DATE_ISO,
    sole_objective_label,
)
from pnl_curve import PnlCurveState

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
    """Single-minded intelligence: only the $95M path exists."""

    def __init__(
        self,
        target_capital: float = TARGET_CAPITAL_USD,
        target_date: str | None = None,
    ) -> None:
        self.target_capital = target_capital
        self.target_date_iso = target_date or TARGET_DATE_ISO
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
    ) -> MissionState:
        progress = self._progress_pct(equity)
        behind = not metrics.on_track or metrics.required_daily_return_pct > 2.5

        schedule_pressure = _clamp01(
            metrics.required_daily_return_pct / 8.0 if behind else metrics.required_daily_return_pct / 15.0
        )
        if behind:
            schedule_pressure = max(schedule_pressure, 0.55)

        curve_vert = curve.verticality if curve else 0.5
        curve_phase = curve.curve_phase if curve else "flat"
        preserve = curve.preserve_capital if curve else False

        mission_focus = _clamp01(
            0.35 * progress
            + 0.25 * curve_vert
            + 0.20 * path_reliability
            + 0.20 * (1.0 - schedule_pressure * 0.5)
        )
        if curve_phase == "declining":
            mission_focus *= 0.55
        elif curve_phase == "vertical":
            mission_focus = min(1.0, mission_focus * 1.12)

        if behind and curve_phase in ("vertical", "climbing"):
            risk_mult = min(1.25, 1.0 + schedule_pressure * 0.2)
            min_conv = 0.54
            directive = "BEHIND SCHEDULE — press only high-conviction compounders toward mission target"
            entry_ok = survival >= 0.2
        elif behind:
            risk_mult = max(0.45, 0.85 - schedule_pressure * 0.35)
            min_conv = 0.68
            directive = "BEHIND SCHEDULE — protect path; elite entries only"
            entry_ok = survival >= 0.35 and path_reliability >= 0.25
        elif preserve:
            risk_mult = 0.5
            min_conv = 0.70
            directive = "PRESERVE COMPOUNDING BASE — vertical curve required before size"
            entry_ok = path_reliability >= 0.4 and curve_vert >= 0.5
        else:
            risk_mult = min(1.15, 0.9 + mission_focus * 0.25)
            min_conv = 0.52
            directive = "ON MISSION — grow equity along required compound path to target"
            entry_ok = True

        if equity < 50 and behind:
            risk_mult = min(1.35, risk_mult * 1.1)
            min_conv = min(min_conv, 0.58)

        state = MissionState(
            sole_objective=SOLE_OBJECTIVE,
            progress_pct=round(progress, 6),
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
                f"conviction {conviction:.3f} below mission floor {st.min_conviction:.3f} for target path",
            )
        if st.behind_schedule and conviction < 0.62 and st.schedule_pressure > 0.7:
            return False, "behind mission schedule — need stronger edge"
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
        gap = metrics.projected_capital_at_target / self.target_capital if metrics else 0
        return (
            f"MISSION BRAIN | sole purpose: {state.sole_objective}\n"
            f"  progress={state.progress_pct:.4f}% of target | equity=${equity:,.2f} | "
            f"need {metrics.required_daily_return_pct:.2f}%/day | {metrics.days_remaining}d left\n"
            f"  focus={state.mission_focus:.0%} schedule_pressure={state.schedule_pressure:.0%} | "
            f"projected_vs_target={gap:.1%}\n"
            f"  >> {state.directive}"
        )

    def _progress_pct(self, equity: float) -> float:
        if equity <= 0 or self.target_capital <= 1:
            return 0.0
        return 100.0 * math.log(max(equity, 1e-9)) / math.log(self.target_capital)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
