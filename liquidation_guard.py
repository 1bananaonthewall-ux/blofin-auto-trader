"""
Stop/TP must sit BETWEEN entry and exchange liquidation (never past liq).

Long:  liq below entry → SL trigger must be ABOVE liq (closer to entry).
Short: liq above entry → SL trigger must be BELOW liq.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DEFAULT_MAINTENANCE = 0.005
# Fraction of entry→liq distance for SL (higher = SL farther from liquidation).
SL_LIQ_BUFFER = 0.44


def achievable_margin_rates(
    settings: object,
    equity: float,
) -> tuple[float, float]:
    """
    Min/target margin_rate for sizing. Micro accounts without margin top-up
    cannot post >100% on min lot at high leverage — use achievable floors.
    """
    from margin_mode import is_cross_margin, normalize_margin_mode

    cfg_min = float(getattr(settings, "min_margin_rate", 1.08) or 1.08)
    cfg_tgt = float(getattr(settings, "target_margin_rate", 1.15) or 1.15)
    micro = float(getattr(settings, "micro_equity_threshold", 10.0) or 10.0)
    small = float(getattr(settings, "small_account_threshold", 50.0) or 50.0)
    top_up = bool(getattr(settings, "margin_top_up_enabled", False))
    mode = normalize_margin_mode(getattr(settings, "margin_mode", "isolated"))

    if is_cross_margin(mode):
        # Cross shares wallet equity; 100% position margin_rate is normal at min lot.
        return (1.0, min(1.12, cfg_tgt))

    if equity > 0 and equity < micro:
        return (1.0, min(1.08, cfg_tgt) if not top_up else min(1.12, cfg_tgt))
    if equity > 0 and equity < small:
        return (max(1.0, min(1.06, cfg_min)), min(1.12, cfg_tgt))
    return (cfg_min, cfg_tgt)


def mission_safe_leverage(
    settings: object,
    exchange_cap: int,
    *,
    planned: int | None = None,
) -> int:
    """
    Exchange leverage for the symbol — NOT capped below mission/scalp max.
    Anti-liquidation is margin_rate (extra isolated collateral), not lower leverage.
    """
    ex_cap = max(1, int(exchange_cap or 1))
    mission = int(getattr(settings, "scalp_leverage_max", 50) or 50)
    if planned is not None and int(planned) > 0:
        return max(3, min(int(planned), ex_cap))
    return max(3, min(mission, ex_cap))


def sl_buffer(settings: object | None = None) -> float:
    if settings is not None:
        return max(0.35, min(0.60, float(getattr(settings, "sl_liq_buffer", SL_LIQ_BUFFER))))
    return SL_LIQ_BUFFER


def liquidation_distance_pct(leverage: int, maintenance: float = DEFAULT_MAINTENANCE) -> float:
    if leverage <= 0:
        return 1.0
    return (1.0 / leverage) + maintenance


def effective_leverage(notional_usdt: float, margin_usdt: float, configured_lev: int) -> int:
    """True leverage from margin actually on the position."""
    if margin_usdt > 0 and notional_usdt > 0:
        eff = int(round(notional_usdt / margin_usdt))
        return max(1, min(eff, 200))
    return max(1, configured_lev)


def margin_rate(notional_usdt: float, margin_usdt: float, configured_lev: int) -> float:
    """1.0 = full initial margin for configured leverage; <1 = under-margined (dangerous)."""
    if notional_usdt <= 0 or configured_lev <= 0:
        return 0.0
    required = notional_usdt / configured_lev
    if required <= 0:
        return 0.0
    return margin_usdt / required


def max_safe_stop_pct(
    leverage: int,
    *,
    maintenance: float = DEFAULT_MAINTENANCE,
    settings: object | None = None,
) -> float:
    return liquidation_distance_pct(leverage, maintenance) * sl_buffer(settings)


def clamp_stop_take_pct(
    stop_pct: float,
    take_pct: float,
    leverage: int,
    *,
    maintenance: float = DEFAULT_MAINTENANCE,
    min_stop_pct: float = 0.003,
    min_rr: float = 1.25,
) -> tuple[float, float]:
    liq = liquidation_distance_pct(leverage, maintenance)
    cap = max_safe_stop_pct(leverage, maintenance=maintenance, settings=None)
    stop = min(max(stop_pct, min_stop_pct), cap)
    take = max(take_pct, stop * min_rr)
    if stop >= liq * 0.9:
        stop = cap
    return stop, take


def enforce_risk_reward(
    stop_pct: float,
    take_pct: float,
    *,
    min_rr: float,
    strict: bool = False,
    max_stop_pct: float | None = None,
    max_take_pct: float | None = None,
    min_stop_pct: float = 0.003,
) -> tuple[float, float] | None:
    """
    Align stop/take to min_rr. strict=True sets take = stop × min_rr (true 3R).
    Returns None when caps cannot fit the ratio.
    """
    if min_rr <= 0 or stop_pct <= 0:
        return None
    stop = max(min_stop_pct, stop_pct)
    if max_stop_pct is not None:
        stop = min(stop, max_stop_pct)
    if strict:
        take = stop * min_rr
        if max_take_pct is not None and take > max_take_pct:
            stop = max_take_pct / min_rr
            if max_stop_pct is not None:
                stop = min(stop, max_stop_pct)
            take = stop * min_rr
            if max_take_pct is not None and take > max_take_pct + 1e-9:
                return None
    else:
        take = max(take_pct, stop * min_rr)
        if max_take_pct is not None:
            take = min(take, max_take_pct)
    if take < stop * min_rr * 0.98:
        return None
    return stop, take


def _cap_tpsl_pcts(
    side: str,
    entry: float,
    stop_pct: float,
    take_pct: float,
    *,
    min_stop_pct: float,
    min_rr: float,
    enforce_tp_from_sl: bool,
    max_stop_pct: float | None,
    max_take_pct: float | None,
) -> tuple[float, float, float, float]:
    stop = max(min_stop_pct, stop_pct)
    if max_stop_pct is not None and max_stop_pct > 0:
        stop = min(stop, max_stop_pct)
    if enforce_tp_from_sl:
        take = stop * min_rr
    else:
        take = max(take_pct, stop * min_rr)
    if max_take_pct is not None and max_take_pct > 0:
        take = min(take, max_take_pct)
        if enforce_tp_from_sl:
            stop = min(stop, take / max(min_rr, 1e-9))
            if max_stop_pct is not None:
                stop = min(stop, max_stop_pct)
            take = stop * min_rr
    if side == "long":
        sl = entry * (1 - stop)
        tp = entry * (1 + take)
    else:
        sl = entry * (1 + stop)
        tp = entry * (1 - take)
    return sl, tp, stop, take


def sl_tp_from_exchange_liq(
    side: str,
    entry: float,
    liquidation_price: float,
    take_pct: float,
    *,
    buffer: float = SL_LIQ_BUFFER,
    min_stop_pct: float = 0.003,
    min_rr: float = 1.25,
    enforce_tp_from_sl: bool = False,
    max_stop_pct: float | None = None,
    max_take_pct: float | None = None,
) -> tuple[float, float, float, float]:
    """
    Place SL/TP using the exchange's liquidation price (ground truth).
    When enforce_tp_from_sl is True, TP distance = stop distance × min_rr (true 3R).
    max_stop_pct / max_take_pct cap wide liq-gap stops (cross margin).
    Returns (sl_price, tp_price, stop_pct, take_pct).
    """
    if entry <= 0:
        return 0.0, 0.0, min_stop_pct, take_pct

    side = side.lower()
    buf = max(0.15, min(0.55, buffer))

    if liquidation_price > 0:
        if side == "long":
            if liquidation_price >= entry:
                log.warning("invalid long liq %.6f >= entry %.6f — fallback formula", liquidation_price, entry)
            else:
                gap = entry - liquidation_price
                sl = entry - gap * buf
                stop_pct = max(min_stop_pct, (entry - sl) / entry)
                if enforce_tp_from_sl:
                    take_pct = stop_pct * min_rr
                else:
                    take_pct = max(take_pct, stop_pct * min_rr)
                return _cap_tpsl_pcts(
                    side,
                    entry,
                    stop_pct,
                    take_pct,
                    min_stop_pct=min_stop_pct,
                    min_rr=min_rr,
                    enforce_tp_from_sl=enforce_tp_from_sl,
                    max_stop_pct=max_stop_pct,
                    max_take_pct=max_take_pct,
                )
        else:
            if liquidation_price <= entry:
                log.warning("invalid short liq %.6f <= entry %.6f — fallback formula", liquidation_price, entry)
            else:
                gap = liquidation_price - entry
                sl = entry + gap * buf
                stop_pct = max(min_stop_pct, (sl - entry) / entry)
                if enforce_tp_from_sl:
                    take_pct = stop_pct * min_rr
                else:
                    take_pct = max(take_pct, stop_pct * min_rr)
                return _cap_tpsl_pcts(
                    side,
                    entry,
                    stop_pct,
                    take_pct,
                    min_stop_pct=min_stop_pct,
                    min_rr=min_rr,
                    enforce_tp_from_sl=enforce_tp_from_sl,
                    max_stop_pct=max_stop_pct,
                    max_take_pct=max_take_pct,
                )

    lev_guess = 20
    sp, tp_pct = clamp_stop_take_pct(min_stop_pct, take_pct, lev_guess, min_rr=min_rr)
    if enforce_tp_from_sl:
        tp_pct = sp * min_rr
    return _cap_tpsl_pcts(
        side,
        entry,
        sp,
        tp_pct,
        min_stop_pct=min_stop_pct,
        min_rr=min_rr,
        enforce_tp_from_sl=enforce_tp_from_sl,
        max_stop_pct=max_stop_pct,
        max_take_pct=max_take_pct,
    )


def tp_target_price(side: str, entry: float, take_pct: float) -> float:
    """Absolute trigger price for take-profit (exchange TPSL / backup close)."""
    if entry <= 0 or take_pct <= 0:
        return 0.0
    side = side.lower()
    if side == "long":
        return entry * (1 + take_pct)
    return entry * (1 - take_pct)


def sl_target_price(side: str, entry: float, stop_pct: float) -> float:
    if entry <= 0 or stop_pct <= 0:
        return 0.0
    side = side.lower()
    if side == "long":
        return entry * (1 - stop_pct)
    return entry * (1 + stop_pct)


def is_sl_trigger_hit(side: str, sl_price: float, price: float) -> bool:
    """True when price crossed an absolute SL trigger (exchange or computed)."""
    if sl_price <= 0 or price <= 0:
        return False
    side_l = side.lower()
    if side_l == "long":
        return price <= sl_price
    return price >= sl_price


def is_tp_trigger_hit(side: str, tp_price: float, price: float) -> bool:
    if tp_price <= 0 or price <= 0:
        return False
    side_l = side.lower()
    if side_l == "long":
        return price >= tp_price
    return price <= tp_price


def is_sl_reached(side: str, entry: float, stop_pct: float, price: float) -> bool:
    sl = sl_target_price(side, entry, stop_pct)
    return is_sl_trigger_hit(side, sl, price)


def is_tp_reached(side: str, entry: float, take_pct: float, price: float) -> bool:
    """True when price has crossed the take-profit level (last/mark)."""
    tp = tp_target_price(side, entry, take_pct)
    return is_tp_trigger_hit(side, tp, price)


def trigger_prices(
    side: str,
    entry: float,
    stop_pct: float,
    take_pct: float,
    leverage: int,
    *,
    min_rr: float = 1.25,
) -> tuple[float, float, float, float]:
    if entry <= 0:
        return 0.0, 0.0, stop_pct, take_pct
    sp, tp = clamp_stop_take_pct(stop_pct, take_pct, leverage, min_rr=min_rr)
    if side == "long":
        return entry * (1 - sp), entry * (1 + tp), sp, tp
    return entry * (1 + sp), entry * (1 - tp), sp, tp


def sl_is_safe(
    side: str,
    entry: float,
    sl_price: float,
    *,
    liquidation_price: float = 0.0,
    leverage: int = 10,
) -> bool:
    if sl_price <= 0 or entry <= 0:
        return False
    liq = liquidation_price
    if liq <= 0:
        liq = (
            entry * (1 - liquidation_distance_pct(leverage))
            if side == "long"
            else entry * (1 + liquidation_distance_pct(leverage))
        )
    if side == "long":
        return sl_price > liq
    return sl_price < liq


def size_for_min_margin_rate(
    *,
    entry_price: float,
    contract_size: float,
    min_size: float,
    target_margin_usdt: float,
    leverage: int,
    min_margin_rate: float,
    margin_budget_usdt: float,
    fixed_leverage: bool = False,
) -> tuple[float, int] | None:
    """
    Pick (contracts, leverage) to deploy target_margin into the position.
    When fixed_leverage=True (50x 3R throughput), never steps leverage down — only size.
    """
    import math

    if entry_price <= 0 or contract_size <= 0 or min_size <= 0 or leverage <= 0:
        return None

    target_margin_usdt = max(target_margin_usdt, min_size * contract_size * entry_price / leverage)
    lev_range = [leverage] if fixed_leverage else range(leverage, 2, -1)

    cushion_rate = max(1.05, min(float(min_margin_rate), 1.40))

    for lev in lev_range:
        # Size as if leverage were lower so posted margin yields margin_rate >= cushion_rate.
        sizing_lev = max(3, int(lev / cushion_rate)) if cushion_rate > 1.01 else lev
        raw = (target_margin_usdt * sizing_lev) / (entry_price * contract_size)
        contracts = max(min_size, math.floor(raw / min_size) * min_size)
        margin = contracts * contract_size * entry_price / lev
        while margin > margin_budget_usdt + 0.001 and contracts >= min_size * 2:
            contracts -= min_size
            margin = contracts * contract_size * entry_price / lev
        notional = contracts * contract_size * entry_price
        mrate = margin_rate(notional, margin, lev)
        if margin <= margin_budget_usdt + 0.001 and mrate >= cushion_rate - 0.02:
            return contracts, lev

    if fixed_leverage:
        min_margin = min_size * contract_size * entry_price / leverage
        notional = min_size * contract_size * entry_price
        mrate = margin_rate(notional, min_margin, leverage)
        if min_margin <= margin_budget_usdt + 0.001 and mrate >= cushion_rate - 0.02:
            return min_size, leverage

    return None


def extra_margin_usdt_for_rate(
    notional_usdt: float,
    leverage: int,
    target_margin_rate: float,
) -> float:
    """USDT to add on isolated position so margin_rate reaches target (e.g. 1.15)."""
    if notional_usdt <= 0 or leverage <= 0:
        return 0.0
    rate = max(1.0, float(target_margin_rate))
    if rate <= 1.0 + 1e-6:
        return 0.0
    base = notional_usdt / leverage
    return max(0.0, base * (rate - 1.0))


def max_notional_for_margin_budget(
    margin_budget_usdt: float,
    leverage: int,
    target_margin_rate: float,
) -> float:
    """Max position notional when posting target_margin_rate × initial margin (e.g. 115%)."""
    if margin_budget_usdt <= 0 or leverage <= 0:
        return 0.0
    rate = max(1.0, float(target_margin_rate))
    return margin_budget_usdt * leverage / rate


def open_stop_within_liq_room(
    stop_pct: float,
    leverage: int,
    *,
    max_fraction_of_liq_dist: float = 0.26,
    maintenance: float = DEFAULT_MAINTENANCE,
) -> bool:
    """True when stop distance leaves cushion before estimated liquidation."""
    if stop_pct <= 0 or leverage <= 0:
        return False
    liq_dist = liquidation_distance_pct(leverage, maintenance)
    cap = max(0.002, liq_dist * max_fraction_of_liq_dist)
    return stop_pct <= cap


def position_over_leverage_cap(
    *,
    inst_lev: int,
    eff_lev: int,
    cap: int,
) -> bool:
    """True when exchange shows leverage above our safe cap (liquidation risk)."""
    if cap <= 0:
        return False
    if inst_lev > cap + 1 or eff_lev > cap + 1:
        return True
    return False


def margin_rate_unsafe(mrate: float, *, min_rate: float = 1.0) -> bool:
    """Under-margined isolated positions liquidate before SL."""
    return mrate > 0 and mrate < min_rate
