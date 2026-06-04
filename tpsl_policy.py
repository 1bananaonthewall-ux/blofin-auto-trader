"""
Unified TP/SL policy: fast 3R throughput, momentum runners, fee floors.

- fast_3r: tight exchange brackets for hourly win rate
- momentum: wider TP on directional runners, capped stop (not liq-gap %)
- scalp: default profile caps

Repair/open paths use the same caps so cross-margin liq distance cannot
inflate stops to 8–30%%.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from margin_mode import is_cross_margin
from liquidation_guard import enforce_risk_reward, sl_tp_from_exchange_liq, trigger_prices
from scalp_profile import profile_for

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

STYLES = ("fast_3r", "momentum", "scalp")


@dataclass(frozen=True)
class TpslPolicy:
    style: str
    max_stop_pct: float
    max_take_pct: float
    min_rr: float
    enforce_tp_from_sl: bool
    min_stop_pct: float
    min_take_pct: float
    fee_coverage_mult: float
    harvest_min_r: float = 0.0
    liq_buffer: float | None = None  # None → settings.sl_liq_buffer

    def sl_tp_kwargs(self) -> dict[str, float | bool]:
        return {
            "min_rr": self.min_rr,
            "enforce_tp_from_sl": self.enforce_tp_from_sl,
            "max_stop_pct": self.max_stop_pct,
            "max_take_pct": self.max_take_pct,
            "min_stop_pct": self.min_stop_pct,
        }


def _fee_min_take_pct(settings: Settings, leverage: int, coverage_mult: float) -> float:
    """Minimum TP %% (price move on notional) to beat round-trip fees."""
    _ = leverage
    rt = float(settings.fee_est_taker_pct) + float(settings.fee_est_maker_pct)
    floor = rt * max(1.0, coverage_mult)
    prof = profile_for(settings)
    scalp_min = prof.min_take_profit_pct if prof else float(settings.min_take_profit_pct)
    return max(scalp_min, floor)


def resolve_tpsl_policy(
    settings: Settings,
    *,
    decision: Any | None = None,
    registry_meta: dict[str, Any] | None = None,
    leverage: int | None = None,
) -> TpslPolicy:
    """Pick brackets from trade style (fast vs momentum vs scalp)."""
    prof = profile_for(settings)
    if not prof:
        lev = leverage or settings.leverage
        return TpslPolicy(
            style="scalp",
            max_stop_pct=0.08,
            max_take_pct=0.15,
            min_rr=1.35,
            enforce_tp_from_sl=False,
            min_stop_pct=0.003,
            min_take_pct=_fee_min_take_pct(settings, lev, 2.0),
            fee_coverage_mult=2.0,
        )

    style = str((registry_meta or {}).get("trade_style") or "").strip().lower()
    if style not in STYLES:
        style = ""

    try:
        from runner_momentum import (
            extended_runner_take_pct,
            is_directional_runner,
            runner_priority_active,
        )

        runner_priority = runner_priority_active(settings)
        cf_runner = (
            is_directional_runner(decision, settings) if decision is not None else False
        )
    except Exception:
        runner_priority = False
        cf_runner = False

    lev = int(
        leverage
        or (registry_meta or {}).get("leverage")
        or prof.max_leverage_cap
        or settings.scalp_leverage_max
    )
    fee_mult = float(prof.fee_coverage_multiple or settings.scalp_fee_coverage_mult)
    min_take = _fee_min_take_pct(settings, lev, fee_mult)

    fast_3r_mode = prof.three_r_mode and (
        getattr(settings, "scalp_fast_3r", False)
        or getattr(settings, "hourly_3r_winner_mode", False)
    )

    if not style:
        if cf_runner and runner_priority:
            style = "momentum"
        elif fast_3r_mode:
            style = "fast_3r"
        elif cf_runner:
            style = "momentum"
        else:
            style = "scalp"

    max_stop = prof.max_stop_pct
    max_take = prof.max_take_pct
    min_rr = prof.min_rr
    enforce = prof.enforce_tp_from_sl
    harvest_r = prof.harvest_min_r

    if style == "fast_3r":
        max_stop = min(
            max_stop,
            float(getattr(settings, "scalp_fast_max_stop_pct", 0.008) or 0.008),
        )
        max_take = min(
            max_take,
            float(getattr(settings, "scalp_fast_max_take_pct", 0.0) or 0)
            or max_stop * min_rr * 1.02,
        )
        harvest_r = (
            min(prof.harvest_min_r, 1.0)
            if not getattr(settings, "stack_winners_mode", True)
            else min(prof.harvest_min_r, 1.35)
        )
    elif style == "momentum":
        max_stop = min(
            max_stop,
            float(getattr(settings, "scalp_momentum_max_stop_pct", 0.014) or 0.014),
        )
        stop_hint = float(
            (registry_meta or {}).get("stop_pct")
            or getattr(decision, "stop_pct", None)
            or max_stop
        )
        ext_take = extended_runner_take_pct(
            max(stop_hint, max_stop * 0.5),
            settings=settings,
            leverage=lev,
        )
        max_take = min(
            float(getattr(settings, "runner_extend_take_pct", 0.06)),
            max(max_take, ext_take, max_stop * float(settings.runner_extend_min_rr)),
        )
        enforce = False
        min_rr = float(getattr(settings, "runner_extend_min_rr", 2.5))
        harvest_r = min(max(prof.harvest_min_r, 1.15), 2.0)
    else:
        max_take = max(max_take, max_stop * min_rr)

    max_take = max(max_take, min_take)
    if enforce:
        max_take = max(max_take, max_stop * min_rr)

    return TpslPolicy(
        style=style,
        max_stop_pct=max_stop,
        max_take_pct=max_take,
        min_rr=min_rr,
        enforce_tp_from_sl=enforce,
        min_stop_pct=max(0.002, min_take * 0.35),
        min_take_pct=min_take,
        fee_coverage_mult=fee_mult,
        harvest_min_r=harvest_r,
    )


def align_stop_take(
    settings: Settings,
    stop_pct: float,
    take_pct: float,
    leverage: int,
    *,
    decision: Any | None = None,
    registry_meta: dict[str, Any] | None = None,
    style: str | None = None,
) -> tuple[float, float, TpslPolicy]:
    """
    Apply style caps + fee floor + R:R. Returns (stop, take, policy).
    """
    policy = resolve_tpsl_policy(
        settings, decision=decision, registry_meta=registry_meta, leverage=leverage
    )
    if style and style in STYLES:
        policy = replace(policy, style=style)

    stop = max(policy.min_stop_pct, float(stop_pct))
    take = max(policy.min_take_pct, float(take_pct))
    stop = min(stop, policy.max_stop_pct)

    if policy.enforce_tp_from_sl:
        rr = enforce_risk_reward(
            stop,
            take,
            min_rr=policy.min_rr,
            strict=True,
            max_stop_pct=policy.max_stop_pct,
            max_take_pct=policy.max_take_pct,
            min_stop_pct=policy.min_stop_pct,
        )
        if rr is None:
            stop = policy.max_stop_pct
            take = min(policy.max_take_pct, stop * policy.min_rr)
        else:
            stop, take = rr
    else:
        take = max(take, stop * policy.min_rr)
        take = min(take, policy.max_take_pct)

    # Fee floor on TP (price-move % on notional, not × leverage)
    rt = float(settings.fee_est_taker_pct) + float(settings.fee_est_maker_pct)
    fee_tp_floor = rt * policy.fee_coverage_mult
    if take < fee_tp_floor:
        take = min(policy.max_take_pct, max(fee_tp_floor, policy.min_take_pct))
    if policy.enforce_tp_from_sl and take < stop * policy.min_rr:
        take = min(policy.max_take_pct, stop * policy.min_rr)

    return stop, take, policy


def fast_lethal_cross_mode(settings: Settings) -> bool:
    """
    Cross + fast 3R throughput: fixed ~1%% stop / ~3%% take — no liq-gap TPSL or liq-room gates.
    """
    if not bool(getattr(settings, "scalp_skip_liq_tpsl", True)):
        return False
    if not is_cross_margin(getattr(settings, "margin_mode", "")):
        return False
    if not (
        getattr(settings, "scalp_fast_3r", False)
        or getattr(settings, "scalp_3r_mode", False)
        or getattr(settings, "hourly_3r_winner_mode", False)
    ):
        return False
    return True


def use_fixed_lethal_tpsl(
    settings: Settings,
    *,
    decision: Any | None = None,
    registry_meta: dict[str, Any] | None = None,
) -> bool:
    """Cross fast 3R repair/open: fixed lethal brackets, not liquidation-distance math."""
    if not fast_lethal_cross_mode(settings):
        return False
    policy = resolve_tpsl_policy(
        settings, decision=decision, registry_meta=registry_meta
    )
    return policy.style == "fast_3r"


def skip_liq_guards_on_entry(
    settings: Settings,
    *,
    decision: Any | None = None,
) -> bool:
    """Entry path: skip open_stop_within_liq_room when cross fast lethal is active."""
    if not fast_lethal_cross_mode(settings):
        return False
    if decision is not None:
        style = str(getattr(decision, "trade_style", "") or "").strip().lower()
        if style == "momentum":
            return False
    return True


def lethal_tpsl_prices(
    settings: Settings,
    side: str,
    entry: float,
    take_hint: float,
    leverage: int,
    *,
    decision: Any | None = None,
    registry_meta: dict[str, Any] | None = None,
    stop_hint: float | None = None,
) -> tuple[float, float, float, float, TpslPolicy]:
    """Fixed 3R lethal brackets — no liquidation-distance math."""
    policy = resolve_tpsl_policy(
        settings, decision=decision, registry_meta=registry_meta, leverage=leverage
    )
    sp_in = float(stop_hint if stop_hint is not None else policy.max_stop_pct)
    tk_in = float(take_hint if take_hint > 0 else policy.max_take_pct)
    sp, tk, policy = align_stop_take(
        settings, sp_in, tk_in, leverage, decision=decision, registry_meta=registry_meta
    )
    sl, tp, sp, tk = trigger_prices(side, entry, sp, tk, leverage, min_rr=policy.min_rr)
    return sl, tp, sp, tk, policy


def exchange_tpsl_from_position(
    settings: Settings,
    side: str,
    entry: float,
    liquidation_price: float,
    take_hint: float,
    leverage: int,
    *,
    decision: Any | None = None,
    registry_meta: dict[str, Any] | None = None,
    stop_hint: float | None = None,
) -> tuple[float, float, float, float, TpslPolicy]:
    """Policy TPSL for repair/attach; fast cross uses fixed lethal (no liq gap)."""
    if use_fixed_lethal_tpsl(settings, decision=decision, registry_meta=registry_meta):
        return lethal_tpsl_prices(
            settings,
            side,
            entry,
            take_hint,
            leverage,
            decision=decision,
            registry_meta=registry_meta,
            stop_hint=stop_hint,
        )

    policy = resolve_tpsl_policy(
        settings, decision=decision, registry_meta=registry_meta, leverage=leverage
    )
    buf = (
        policy.liq_buffer
        if policy.liq_buffer is not None
        else float(getattr(settings, "sl_liq_buffer", 0.44))
    )
    if policy.style == "fast_3r":
        buf = min(buf, 0.28)
    elif policy.style == "momentum":
        buf = min(buf, 0.36)

    hint_stop, hint_take, _ = align_stop_take(
        settings,
        policy.max_stop_pct * 0.85,
        take_hint or policy.max_take_pct,
        leverage,
        decision=decision,
        registry_meta=registry_meta,
    )
    sl, tp, sp, tk = sl_tp_from_exchange_liq(
        side,
        entry,
        liquidation_price,
        hint_take,
        buffer=buf,
        **policy.sl_tp_kwargs(),
    )
    sp, tk, _ = align_stop_take(
        settings, sp, tk, leverage, decision=decision, registry_meta=registry_meta
    )
    sl, tp, _, _ = trigger_prices(side, entry, sp, tk, leverage, min_rr=policy.min_rr)
    return sl, tp, sp, tk, policy
