"""
Core brain — single local intelligence for the entire bot.

Mission (mission_config), throughput (3–12 quality 3R/hr), leverage (50x),
open-book health (3R SL/TP repair + smart upgrade), entry policy, optional
Markov regime filter (Hamilton-style latent states), and local swarm votes — one
evaluate() per tick. No paid LLM APIs; runs entirely on your machine.

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
from markov_regime import MarkovSnapshot
from mission_brain import MissionBrain, SOLE_OBJECTIVE
from leverage_intel import leverage_needs_reentry
from liquidation_guard import mission_safe_leverage
from mission_config import TARGET_DAILY_GROWTH_PCT, progress_toward_daily_goal_pct
from pnl_curve import PnlCurveState
from position_registry import PositionRegistry
from hourly_3r import (
    hourly_3r_active,
    is_entry_starved,
    is_opens_starved,
    target_min_opens_per_hour,
    target_wins_per_hour,
)
import api_backoff
from scalp_optimizer import get_active_tuning

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

    def __init__(self) -> None:
        self._mission = MissionBrain()
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
        entries_never_pause: bool = False,
        markov: MarkovSnapshot | None = None,
    ) -> CoreDirective:
        self._entries_never_pause = entries_never_pause
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
            if entries_never_pause:
                entry_ok = equity > 0 and free_margin > 0.01

        from account_guard import effective_hourly_tph_cap, universe_fill_active

        fill_mode = universe_fill_active(settings)
        tmin = settings.optimizer_target_min_tph
        tmax = effective_hourly_tph_cap(settings)
        tuning = get_active_tuning()
        if fill_mode:
            starved = free_margin > settings.margin_reserve_usdt * 2
            overheating = False
        elif hourly_3r_active(settings):
            w_need = target_wins_per_hour(settings)
            o_need = target_min_opens_per_hour(settings)
            starved = tuning.wins_last_hour < w_need or opens_last_hour < o_need
            overheating = False
        else:
            starved = opens_last_hour < tmin
            overheating = opens_last_hour > tmax

        from tpsl_pacing import use_tpsl_only_pacing

        gap = float(settings.scalp_entry_gap_seconds)
        if use_tpsl_only_pacing(settings):
            gap = float(getattr(settings, "tpsl_pace_base_gap_seconds", 2.0))
        elif starved:
            gap = max(6.0, gap - 8.0)
        elif overheating:
            gap = min(50.0, gap + 10.0)

        target_lev = mission_safe_leverage(
            settings, int(settings.scalp_leverage_max)
        )
        allow_fallback = starved or not settings.winner_apex_preferred
        wins_starved = hourly_3r_active(settings) and tuning.wins_last_hour < target_wins_per_hour(
            settings
        )
        if behind and not starved and not wins_starved and not entries_never_pause:
            allow_fallback = False
            min_conv = max(min_conv, settings.winner_elite_score - 0.06)
        elif wins_starved or is_opens_starved(settings, tuning):
            min_conv = min(min_conv, 0.52)
            allow_fallback = True
        if entries_never_pause and getattr(settings, "winner_only_mode", False):
            if fill_mode or starved or is_opens_starved(settings, tuning):
                min_conv = min(min_conv, 0.44)
                allow_fallback = True

        if markov is not None:
            if markov.state == "stress":
                if not starved:
                    min_conv = min(0.92, min_conv + 0.06)
                    gap = min(55.0, gap + 8.0)
                    risk_mult *= 0.88
                else:
                    min_conv = max(0.46, min_conv - 0.02)
                    gap = max(6.0, gap - 2.0)
            elif markov.state == "trend" and starved:
                min_conv = max(0.48, min_conv - 0.02)
                gap = max(6.0, gap - 2.0)
            if markov.transition_risk >= 0.12:
                min_conv = min(0.94, min_conv + markov.transition_risk * 0.12)
            stress_pause = markov.probs[2] > 0.42
            if equity < settings.small_account_threshold:
                stress_pause = markov.probs[2] > 0.34
            if (
                stress_pause
                and not unrestricted
                and not entries_never_pause
                and not fill_mode
                and not (
                    hourly_3r_active(settings)
                    and (is_entry_starved(settings) or is_opens_starved(settings))
                )
            ):
                entry_ok = False
                mission_dir = f"Markov stress {markov.probs[2]:.0%} — entries paused"

        upgrade_closes = 0
        if (
            settings.leverage_auto_upgrade
            and not getattr(settings, "stack_winners_mode", True)
            and open_count > 0
            and low_leverage_positions > 0
        ):
            if starved:
                upgrade_closes = 1
            elif low_leverage_positions >= 3:
                upgrade_closes = 1

        parts: list[str] = []
        if starved:
            if hourly_3r_active(settings):
                w_need = target_wins_per_hour(settings)
                o_need = target_min_opens_per_hour(settings)
                parts.append(
                    f"STARVED wins={tuning.wins_last_hour}/{w_need} opens={opens_last_hour}/{o_need} gap={gap:.0f}s"
                )
            else:
                parts.append(f"STARVED {opens_last_hour}/{tmin} tph gap={gap:.0f}s")
        elif overheating:
            parts.append(f"HOT {opens_last_hour}>{tmax}/hr")
        else:
            parts.append(f"PACE {opens_last_hour} tph")
        parts.append(f"{target_lev}x 3R")
        if low_leverage_positions:
            parts.append(f"{low_leverage_positions} under-lev")
        if markov is not None:
            parts.append(f"mkov={markov.state}")
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
            force_max_leverage_on_open=bool(settings.scalp_3r_mode)
            and settings.max_effective_leverage >= settings.scalp_leverage_max - 1,
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
        floor = d.min_conviction
        if getattr(self, "_entries_never_pause", False) and d.starved:
            floor = min(floor, 0.46)
        if conviction < floor:
            return (
                False,
                f"conviction {conviction:.3f} < core floor {floor:.3f}",
            )
        if d.behind_schedule and conviction < 0.62 and not d.starved and not getattr(
            self, "_entries_never_pause", False
        ):
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
        day_start = (
            metrics.projected_capital_at_target / (1 + TARGET_DAILY_GROWTH_PCT / 100)
            if metrics and metrics.projected_capital_at_target > 0
            else 0.0
        )
        today_pct = (equity / day_start - 1.0) * 100.0 if day_start > 0 and equity > 0 else 0.0
        progress = progress_toward_daily_goal_pct(today_pct)
        return (
            f"CORE BRAIN | {d.sole_objective}\n"
            f"  equity=${equity:,.2f} today={today_pct:+.2f}% ({progress:.1f}% of +{TARGET_DAILY_GROWTH_PCT:.0f}% goal) | "
            f"EOD target=${metrics.projected_capital_at_target:,.2f}\n"
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
        tracker=None,
    ) -> BookReport:
        """Maintain every open trade: 50x setting + 3R SL/TP; upgrade under-levered slots."""
        d = self._last
        starved = d.starved if d else False
        max_closes = max_closes if max_closes is not None else (d.max_upgrade_closes if d else 1)

        positions = ex.fetch_all_positions()
        if api_backoff.is_paused():
            n = len(positions)
            return BookReport(
                open_count=n,
                sltp_repaired=0,
                leverage_set=0,
                upgraded_closed=0,
                healthy=n,
                under_levered=0,
            )
        sltp_n = lev_n = closed = healthy = under = 0
        closes_left = max_closes

        for symbol, pos in list(positions.items()):
            side = pos.get("side")
            contracts = float(pos.get("contracts") or 0)
            entry = float(pos.get("entry_price") or 0)
            if not side or contracts <= 0 or entry <= 0:
                continue

            trade_sym = str(pos.get("symbol") or symbol).split("#", 1)[0]
            meta = registry.get(trade_sym)
            exchange_cap = ex.symbol_leverage_cap(trade_sym)
            registry_lev = int((meta or {}).get("leverage") or 0)
            symbol_target = mission_safe_leverage(
                settings, exchange_cap, planned=registry_lev or None
            )
            exchange_max = ex.leverage_intel.exchange_max(trade_sym) or exchange_cap

            eff_lev = int(pos.get("effective_leverage") or pos.get("leverage") or 0)
            inst_lev = int(pos.get("leverage") or 0)
            age = _position_age(registry, trade_sym)
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
                    trade_sym,
                    side,
                    contracts,
                    take_pct=take_pct,
                    configured_leverage=symbol_target,
                    dry_run=settings.dry_run,
                    cancel_existing=False,
                    registry_meta=meta,
                )
                if ok and rep_stop > 0 and rep_take > 0:
                    sl_px, tp_px = 0.0, 0.0
                    prices = getattr(ex, "last_repaired_tpsl_prices", None)
                    if prices and len(prices) >= 2:
                        sl_px, tp_px = float(prices[0]), float(prices[1])
                    registry.update_tpsl(
                        trade_sym,
                        stop_pct=rep_stop,
                        take_pct=rep_take,
                        sl_price=sl_px,
                        tp_price=tp_px,
                    )
                    if meta:
                        registry.update_leverage(
                            trade_sym, leverage=applied or symbol_target
                        )
                    sltp_n += 1
                    rr = rep_take / max(rep_stop, 1e-9)
                    log.info(
                        "CORE %s %s | %dx cap 3R rr=%.2f:1 stop=%.2f%% take=%.2f%% inst=%dx eff=%dx | exchange TP/SL live",
                        symbol.split("/")[0],
                        side,
                        symbol_target,
                        rr,
                        rep_stop * 100,
                        rep_take * 100,
                        inst_lev,
                        eff_lev,
                    )
                else:
                    log.warning(
                        "CORE %s %s — NO exchange TP/SL (repair failed; steward will retry)",
                        symbol.split("/")[0],
                        side,
                    )
            except Exception:
                log.exception("core book SL/TP %s", symbol)

            pos = ex._lookup_open_position(trade_sym, side) or pos
            eff_lev = int(pos.get("effective_leverage") or eff_lev)
            inst_lev = int(pos.get("leverage") or inst_lev)

            if inst_lev >= symbol_target - 2 and eff_lev >= symbol_target - 3:
                healthy += 1
                continue

            under += 1
            needs_close, reason = leverage_needs_reentry(
                pos, target_lev=symbol_target, exchange_max=exchange_max
            )
            if not needs_close:
                continue
            if age < 90 or closes_left <= 0 or not settings.leverage_auto_upgrade:
                continue
            if getattr(settings, "stack_winners_mode", True):
                continue
            if not starved and eff_lev >= symbol_target // 2 and inst_lev >= symbol_target - 2:
                continue
            mark_px = float(pos.get("mark_price") or entry)
            if ex.stream:
                spx = ex.stream.get_last_price(symbol)
                if spx and spx > 0:
                    mark_px = float(spx)
            side_l = str(pos.get("side") or "long").lower()
            gross = (
                (mark_px - entry) / entry
                if side_l == "long"
                else (entry - mark_px) / entry
            )
            if gross > 0.003:
                continue

            try:
                meta = registry.get(symbol) or {}
                side_s = str(pos.get("side") or meta.get("side") or "long")
                stop_pct_m = float(meta.get("stop_pct") or take_pct * 0.35)
                take_pct_m = float(meta.get("take_pct") or take_pct)
                close_px = float(pos.get("mark_price") or entry)
                if ex.stream:
                    px = ex.stream.get_last_price(symbol)
                    if px and px > 0:
                        close_px = float(px)
                ex.close_position(symbol, pos, settings.dry_run)
                registry.remove(symbol)
                if not settings.dry_run:
                    from ml.outcomes import notify_trade_close

                    notify_trade_close(
                        tracker,
                        symbol,
                        side_s,
                        entry,
                        close_px,
                        stop_pct=stop_pct_m,
                        take_pct=take_pct_m,
                        reason=f"upgrade_close:{reason}",
                    )
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

    def should_run_book_pass(self, open_count: int, *, interval_sec: float = 20.0) -> bool:
        if open_count <= 0:
            return False
        return (time.time() - self._last_book_pass) >= interval_sec
