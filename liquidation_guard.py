"""
Stop/TP must sit BETWEEN entry and exchange liquidation (never past liq).

Long:  liq below entry → SL trigger must be ABOVE liq (closer to entry).
Short: liq above entry → SL trigger must be BELOW liq.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DEFAULT_MAINTENANCE = 0.005
# Fraction of entry→liq distance where we place SL (0.35 = 35% from entry toward liq).
SL_LIQ_BUFFER = 0.38


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


def max_safe_stop_pct(leverage: int, *, maintenance: float = DEFAULT_MAINTENANCE) -> float:
    return liquidation_distance_pct(leverage, maintenance) * SL_LIQ_BUFFER


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
    cap = max_safe_stop_pct(leverage, maintenance=maintenance)
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
) -> tuple[float, float, float, float]:
    """
    Place SL/TP using the exchange's liquidation price (ground truth).
    When enforce_tp_from_sl is True, TP distance = stop distance × min_rr (true 3R).
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
                tp = entry * (1 + take_pct)
                return sl, tp, stop_pct, take_pct
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
                tp = entry * (1 - take_pct)
                return sl, tp, stop_pct, take_pct

    lev_guess = 20
    sp, tp_pct = clamp_stop_take_pct(min_stop_pct, take_pct, lev_guess, min_rr=min_rr)
    if enforce_tp_from_sl:
        tp_pct = sp * min_rr
    sl, tp_p, sp, tp_pct = trigger_prices(side, entry, sp, tp_pct, lev_guess, min_rr=min_rr)
    return sl, tp_p, sp, tp_pct


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

    for lev in lev_range:
        raw = (target_margin_usdt * lev) / (entry_price * contract_size)
        contracts = max(min_size, math.floor(raw / min_size) * min_size)
        margin = contracts * contract_size * entry_price / lev
        while margin > margin_budget_usdt + 0.001 and contracts >= min_size * 2:
            contracts -= min_size
            margin = contracts * contract_size * entry_price / lev
        if margin <= margin_budget_usdt + 0.001 and margin >= target_margin_usdt * min_margin_rate * 0.9:
            return contracts, lev

    if fixed_leverage:
        min_margin = min_size * contract_size * entry_price / leverage
        if min_margin <= margin_budget_usdt + 0.001:
            return min_size, leverage

    return None
