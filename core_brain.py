"""
Core brain — single local intelligence for the entire bot.

Mission ($95M by 2027), throughput (3–12 quality 3R/hr), leverage (50x),
open-book health (3R SL/TP repair + smart upgrade), and entry policy — one
evaluate() per tick. No paid APIs; runs entirely on your machine.

Ancillary modules (mission_brain, throughput_brain, position_brain) are thin
wrappers for compatibility; wire everything through AutonomousGrowthEngine.core.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from growth_optimizer import GrowthMetrics
from mission_brain import MissionBrain, SOLE_OBJECTIVE
from leverage_intel import leverage_needs_reentry
from mission_config import TARGET_CAPITAL_USD, TARGET_DATE_ISO
from pnl_curve import PnlCurveState
from position_registry import PositionRegistry

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings
    from exchange_client import BlofinExchange
    from fluid_manifold import FluidSnapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoreDirective:
    """Unified decision surface — everything the bot needs from one tick."""

    sole_objective: str
    mission_directive: str
    mission_focus: float
    min_conviction: float
    risk_multiplier: float
    entry_allowed: bool
    behind_schedule: bool
    opens_last_hour: int
    starved: bool
    overheating: bool
    target_entry_gap: float
    target_leverage: int
    allow_elite_fallback: bool
    maintain_open_book: bool
    max_upgrade_closes: int
    force_max_leverage_on_open: bool
    summary: str


@dataclass
class BookReport:
    open_count: int
    sltp_repaired: int
    leverage_set: int
    upgraded_closed: int
    healthy: int
    under_levered: int


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _position_age(registry: PositionRegistry, symbol: str) -> float:
    meta = registry.get(symbol)
    if not meta:
        return 9999.0
    return max(0.0, time.time() - float(meta.get("opened_at", 0)))


class CoreBrain:
    """
    One brain to rule pacing, mission, book health, and entry quality.
    """

    def __init__(
        self,
        target_capital: float = TARGET_CAPITAL_USD,
        target_date: str | None = None,
    ) -> None:
        self.target_capital = target_capital
        self.target_date_iso = target_date or TARGET_DATE_ISO
        self._mission = MissionBrain(target_capital, target_date)
        self._last: CoreDirective | None = None
        self._last_book_pass = 0.0

    @property
    def last(self) -> CoreDirective | None:
        return self._last

    def evaluate(
        self,
        settings: "Settings",
        *,
        equity: float,
        free_margin: float,
        metrics: GrowthMetrics,
        curve: PnlCurveState | None,
        fluid: "FluidSnapshot | None",
        opens_last_hour: int,
        open_count: int,
        low_leverage_positions: int = 0,
        unrestricted: bool = False,
    ) -> CoreDirective:
        path_rel = fluid.path_reliability if fluid else 0.5
        survival = fluid.survival if fluid else 0.5

        if unrestricted:
            mission_dir = "UNRESTRICTED — mission pauses off; 50x 3R throughput priority"
            min_conv = 0.52
            entry_ok = equity > 0 and free_margin > 0.01
            risk_mult = min(1.35, 0.9 + metrics.aggression_boost * 0.3)
            mission_focus = 0.65
            behind = not metrics.on_track
        else:
            ms = self._mission.evaluate(
                equity, metrics, curve, path_reliability=path_rel, survival=survival
            )
            mission_dir = ms.directive
            min_conv = ms.min_conviction
            entry_ok = ms.entry_allowed
            risk_mult = ms.risk_multiplier
            mission_focus = ms.mission_focus
            behind = ms.behind_schedule

        tmin = settings.optimizer_target_min_tph
        tmax = settings.optimizer_target_max_tph
        starved = opens_last_hour < tmin
        overheating = opens_last_hour > tmax

        gap = float(settings.scalp_entry_gap_seconds)
        if starved:
            gap = max(6.0, gap - 8.0)
        elif overheating:
            gap = min(50.0, gap + 10.0)

        target_lev = int(settings.scalp_leverage_max)
        allow_fallback = starved or not settings.winner_apex_preferred
        if behind and not starved:
            allow_fallback = False
            min_conv = max(min_conv, settings.winner_elite_score - 0.06)

        upgrade_closes = 0
        if settings.leverage_auto_upgrade and open_count > 0 and low_leverage_positions > 0:
            if starved:
                upgrade_closes = 2
            elif low_leverage_positions >= 2:
                upgrade_closes = 1

        parts: list[str] = []
        if starved:
            parts.append(f"STARVED {opens_last_hour}/{tmin} tph gap={gap:.0f}s")
        elif overheating:
            parts.append(f"HOT {opens_last_hour}>{tmax}/hr")
        else:
            parts.append(f"PACE {opens_last_hour} tph")
        parts.append(f"{target_lev}x 3R")
        if low_leverage_positions:
            parts.append(f"{low_leverage_positions} under-lev")
        parts.append(mission_dir.split("—")[-1].strip()[:40])

        directive = CoreDirective(
            sole_objective=SOLE_OBJECTIVE,
            mission_directive=mission_dir,
            mission_focus=round(mission_focus, 4),
            min_conviction=round(min_conv, 3),
            risk_multiplier=round(risk_mult, 3),
            entry_allowed=entry_ok,
            behind_schedule=behind,
            opens_last_hour=opens_last_hour,
            starved=starved,
            overheating=overheating,
            target_entry_gap=gap,
            target_leverage=target_lev,
            allow_elite_fallback=allow_fallback,
            maintain_open_book=open_count > 0,
            max_upgrade_closes=upgrade_closes,
            force_max_leverage_on_open=bool(settings.scalp_3r_mode),
            summary=" | ".join(parts),
        )
        self._last = directive
        return directive

    def permits_trade(self, conviction: float) -> tuple[bool, str]:
        d = self._last
        if d is None:
            return False, "core brain not initialized"
        if not d.entry_allowed:
            return False, d.mission_directive
        if conviction < d.min_conviction:
            return (
                False,
                f"conviction {conviction:.3f} < core floor {d.min_conviction:.3f}",
            )
        if d.behind_schedule and conviction < 0.62 and not d.starved:
            return False, "behind mission — need apex-level edge"
        return True, "core brain: advances mission at target pace"

    def scale_conviction(self, raw: float) -> float:
        d = self._last
        if d is None:
            return raw
        scaled = raw * (0.7 + 0.3 * d.mission_focus)
        if d.starved and raw >= d.min_conviction:
            scaled = min(1.0, scaled * 1.04)
        if d.behind_schedule and raw >= d.min_conviction + 0.06:
            scaled = min(1.0, scaled * 1.06)
        return round(scaled, 4)

    def format_status(self, equity: float, metrics: GrowthMetrics) -> str:
        d = self._last
        if d is None:
            return "CORE BRAIN | not yet evaluated"
        gap_pct = metrics.projected_capital_at_target / self.target_capital if metrics else 0
        progress = (
            100.0 * math.log(max(equity, 1e-9)) / math.log(self.target_capital)
            if equity > 0 and self.target_capital > 1
            else 0.0
        )
        return (
            f"CORE BRAIN | {d.sole_objective}\n"
            f"  equity=${equity:,.2f} progress={progress:.4f}% | "
            f"need {metrics.required_daily_return_pct:.2f}%/day | {metrics.days_remaining}d\n"
            f"  {d.summary} | focus={d.mission_focus:.0%}\n"
            f"  >> {d.mission_directive}"
        )

    def reconcile_book(
        self,
        ex: "BlofinExchange",
        settings: "Settings",
        registry: PositionRegistry,
        *,
        max_closes: int | None = None,
    ) -> BookReport:
        """Maintain every open trade: 50x setting + 3R SL/TP; upgrade under-levered slots."""
        d = self._last
        mission_lev = int(settings.scalp_leverage_max)
        starved = d.starved if d else False
        max_closes = max_closes if max_closes is not None else (d.max_upgrade_closes if d else 1)

        positions = ex.fetch_all_positions()
        sltp_n = lev_n = closed = healthy = under = 0
        closes_left = max_closes

        for symbol, pos in list(positions.items()):
            symbol_target = ex.symbol_leverage_cap(symbol)
            exchange_max = ex.leverage_intel.exchange_max(symbol) or symbol_target
            side = pos.get("side")
            contracts = float(pos.get("contracts") or 0)
            entry = float(pos.get("entry_price") or 0)
            if not side or contracts <= 0 or entry <= 0:
                continue

            eff_lev = int(pos.get("effective_leverage") or pos.get("leverage") or 0)
            inst_lev = int(pos.get("leverage") or 0)
            age = _position_age(registry, symbol)
            meta = registry.get(symbol)
            take_pct = float(
                pos.get("take_pct")
                or (meta or {}).get("take_pct")
                or settings.scalp_max_stop_pct * settings.scalp_3r_min_rr
            )
            pos_side = "long" if side == "long" else "short"

            applied = symbol_target
            try:
                applied = ex.ensure_leverage(symbol, pos_side, leverage=symbol_target) or symbol_target
                if applied:
                    lev_n += 1
            except Exception as exc:
                log.debug("core book: lev %s: %s", symbol.split("/")[0], exc)

            try:
                ok, rep_stop, rep_take = ex.repair_position_tpsl(
                    symbol,
                    side,
                    contracts,
                    take_pct=take_pct,
                    configured_leverage=symbol_target,
                    dry_run=settings.dry_run,
                    cancel_existing=True,
                )
                if ok and rep_stop > 0 and rep_take > 0:
                    registry.update_tpsl(symbol, stop_pct=rep_stop, take_pct=rep_take)
                    if meta:
                        registry.update_leverage(symbol, leverage=applied or symbol_target)
                    sltp_n += 1
                    rr = rep_take / max(rep_stop, 1e-9)
                    log.info(
                        "CORE %s %s | %dx cap 3R rr=%.2f:1 stop=%.2f%% take=%.2f%% inst=%dx eff=%dx",
                        symbol.split("/")[0],
                        side,
                        symbol_target,
                        rr,
                        rep_stop * 100,
                        rep_take * 100,
                        inst_lev,
                        eff_lev,
                    )
            except Exception:
                log.exception("core book SL/TP %s", symbol)

            pos = ex.fetch_all_positions().get(symbol) or pos
            eff_lev = int(pos.get("effective_leverage") or eff_lev)
            inst_lev = int(pos.get("leverage") or inst_lev)

            if inst_lev >= symbol_target - 2 and eff_lev >= symbol_target - 3:
                healthy += 1
                continue

            under += 1
            needs_close, reason = leverage_needs_reentry(
                pos, target_lev=mission_lev, exchange_max=exchange_max
            )
            if not needs_close:
                continue
            if age < 90 or closes_left <= 0 or not settings.leverage_auto_upgrade:
                continue
            if not starved and eff_lev >= symbol_target // 2 and inst_lev >= symbol_target - 2:
                continue

            try:
                ex.close_position(symbol, pos, settings.dry_run)
                registry.remove(symbol)
                closed += 1
                closes_left -= 1
                log.warning(
                    "CORE upgrade-close %s (%s) inst=%dx eff=%dx -> re-enter %dx",
                    symbol.split("/")[0],
                    reason,
                    inst_lev,
                    eff_lev,
                    symbol_target,
                )
                time.sleep(0.2)
            except Exception:
                log.exception("core upgrade close %s", symbol)

        self._last_book_pass = time.time()
        if positions:
            log.info(
                "CORE book %d | healthy=%d under=%d | sltp=%d lev=%d upgraded=%d",
                len(positions),
                healthy,
                under,
                sltp_n,
                lev_n,
                closed,
            )

        return BookReport(
            open_count=len(positions),
            sltp_repaired=sltp_n,
            leverage_set=lev_n,
            upgraded_closed=closed,
            healthy=healthy,
            under_levered=under,
        )

    def should_run_book_pass(self, open_count: int, *, interval_sec: float = 4.0) -> bool:
        if open_count <= 0:
            return False
        return (time.time() - self._last_book_pass) >= interval_sec
