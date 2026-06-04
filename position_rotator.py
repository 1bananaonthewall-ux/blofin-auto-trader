"""
Harvest fee-matured winners and upgrade into higher-conviction setups.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from exchange_client import BlofinExchange
from fee_engine import analyze_trade_fees
from markets import Market
from position_registry import PositionRegistry

log = logging.getLogger(__name__)

MIN_HOLD_SECONDS = 90.0
FEE_COVERAGE_MULT = 2.2
UPGRADE_CONVICTION_GAP = 0.12
LOSER_UPGRADE_GAP = 0.05
SEMI_LOSER_UPGRADE_GAP = 0.07


@dataclass
class RotationAction:
    symbol: str
    action: str  # harvest | upgrade_out
    reason: str
    pnl_after_fees_usd: float


def _gross_pnl_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if side == "long":
        return (price - entry) / entry
    return (entry - price) / entry


def evaluate_harvest(
    symbol: str,
    pos: dict,
    meta: dict | None,
    last_price: float,
    market: Market,
    *,
    fee_taker: float,
    fee_maker: float,
    default_leverage: int,
    harvest_eagerness: float = 1.0,
    min_hold_seconds: float = MIN_HOLD_SECONDS,
    fee_coverage_mult: float = FEE_COVERAGE_MULT,
    harvest_min_r: float = 0.0,
    stack_winners: bool = False,
    early_harvest: bool = False,
) -> RotationAction | None:
    entry = float(pos.get("entry_price") or (meta or {}).get("entry_price") or 0)
    side = pos.get("side") or "long"
    contracts = float(pos.get("contracts") or 0)
    if entry <= 0 or contracts <= 0 or last_price <= 0:
        return None

    opened_at = float((meta or {}).get("opened_at") or 0)
    if opened_at and (time.time() - opened_at) < min_hold_seconds:
        return None

    lev = int((meta or {}).get("leverage") or default_leverage)
    stop_pct = float((meta or {}).get("stop_pct") or 0.012)
    take_pct = float((meta or {}).get("take_pct") or 0.022)
    gross = _gross_pnl_pct(side, entry, last_price)

    # Stack-winners: let exchange TP fill; discretionary harvest only at full target.
    if stack_winners and not early_harvest:
        if take_pct <= 0 or gross < take_pct * 0.998:
            return None

    # Backup when exchange TPSL lags: price already at/through TP target.
    fast_3r = harvest_min_r > 0 and harvest_min_r <= 2.25
    if stack_winners and not early_harvest:
        tp_hit_frac = 0.998
    elif early_harvest:
        tp_hit_frac = 0.92 if fast_3r else 0.97
    else:
        tp_hit_frac = 0.985 if fast_3r else 0.99
    if take_pct > 0 and gross >= take_pct * tp_hit_frac:
        fee_quick = analyze_trade_fees(
            entry,
            contracts,
            market.contract_size,
            stop_pct,
            max(take_pct, gross),
            lev,
            taker_fee=fee_taker,
            maker_fee=fee_maker,
        )
        if fee_quick.profit_after_fees_usd > 0:
            return RotationAction(
                symbol=symbol,
                action="harvest",
                reason=f"tp-zone gross={gross:.2%} target={take_pct:.2%} net=${fee_quick.profit_after_fees_usd:.4f}",
                pnl_after_fees_usd=fee_quick.profit_after_fees_usd,
            )

    fee = analyze_trade_fees(
        entry,
        contracts,
        market.contract_size,
        stop_pct,
        max(take_pct, gross) if gross > 0 else take_pct,
        lev,
        taker_fee=fee_taker,
        maker_fee=fee_maker,
    )

    if not fee.fee_covered or fee.profit_after_fees_usd <= 0:
        return None

    r_multiple = gross / stop_pct if stop_pct > 0 else 0.0
    if harvest_min_r > 0 and r_multiple < harvest_min_r:
        return None
    if stack_winners and not early_harvest and take_pct > 0 and gross < take_pct * 0.998:
        return None

    tp_progress = gross / take_pct if take_pct > 0 else 0
    eagerness = max(0.75, min(1.15 if stack_winners else 1.8, harvest_eagerness))
    fee_mult = fee_coverage_mult / eagerness
    target_rr = take_pct / max(stop_pct, 1e-9)
    if harvest_min_r > 0:
        tp_bar = max(0.80, harvest_min_r / max(target_rr, 1.0))
        tp_gross_bar = tp_bar
    else:
        tp_bar = max(0.22, 0.42 / eagerness)
        tp_gross_bar = max(0.45, 0.62 / eagerness)
    matured = (
        fee.profit_after_fees_usd >= fee.total_fee_usd * fee_mult
        and gross >= fee.min_profit_to_beat_fees_pct / max(lev, 1)
        and (tp_progress >= tp_bar or gross >= take_pct * tp_gross_bar)
    )
    if not matured:
        return None

    return RotationAction(
        symbol=symbol,
        action="harvest",
        reason=f"fee-matured gross={gross:.2%} net=${fee.profit_after_fees_usd:.4f}",
        pnl_after_fees_usd=fee.profit_after_fees_usd,
    )


def find_upgrade_victim(
    open_positions: dict[str, dict],
    registry: PositionRegistry,
    best_new_conviction: float,
    best_new_symbol: str,
    stream_prices: dict[str, float],
    markets: dict[str, Market],
) -> RotationAction | None:
    """Free margin by closing the weakest open slot if a much stronger setup waits."""
    if best_new_conviction < 0.55:
        return None

    weakest: tuple[str, float, dict] | None = None
    for sym, pos in open_positions.items():
        if sym == best_new_symbol:
            continue
        meta = registry.get(sym)
        price = stream_prices.get(sym) or float(pos.get("entry_price") or 0)
        entry = float(pos.get("entry_price") or (meta or {}).get("entry_price") or 0)
        side = pos.get("side") or "long"
        gross = _gross_pnl_pct(side, entry, price)
        held_conv = float((meta or {}).get("conviction") or 0.45)
        rank_conv = held_conv
        # Deprioritize positions that are already strong winners (let them run to TP)
        if gross > 0.015:
            rank_conv += 0.15
        elif gross < -0.004:
            rank_conv -= 0.10
        elif gross < 0:
            rank_conv -= 0.05
        if weakest is None or rank_conv < weakest[1]:
            weakest = (sym, rank_conv, pos, gross, held_conv)

    if weakest is None:
        return None
    sym, _rank_conv, pos, gross, held_conv = weakest
    # Never evict winners to make room — only free slots from flat/losing books.
    if gross > 0.002:
        return None
    if gross < -0.004:
        gap = SEMI_LOSER_UPGRADE_GAP
    elif gross < 0:
        gap = LOSER_UPGRADE_GAP
    else:
        gap = UPGRADE_CONVICTION_GAP
    if best_new_conviction < held_conv + gap:
        return None

    return RotationAction(
        symbol=sym,
        action="upgrade_out",
        reason=f"upgrade for {best_new_symbol} conv {best_new_conviction:.3f}>{held_conv:.3f}",
        pnl_after_fees_usd=0.0,
    )


def _position_already_closed(exc: BaseException) -> bool:
    """Exchange close when flat (TPSL filled first) — not a rotation failure."""
    msg = str(exc).lower()
    return "102005" in msg or "had been closed" in msg or "position does not exist" in msg


def execute_rotation(
    ex: BlofinExchange,
    action: RotationAction,
    open_positions: dict[str, dict],
    registry: PositionRegistry,
    dry_run: bool,
    tracker=None,
) -> bool:
    pos = open_positions.get(action.symbol)
    if not pos:
        return False
    log.info("ROTATE %s %s: %s", action.action, action.symbol, action.reason)
    if dry_run:
        registry.remove(action.symbol)
        return True
    try:
        meta = registry.get(action.symbol) or {}
        entry = float(pos.get("entry_price") or meta.get("entry_price") or 0)
        side = pos.get("side") or meta.get("side") or "long"
        stop_pct = float(meta.get("stop_pct") or 0.012)
        take_pct = float(meta.get("take_pct") or 0.022)
        ex.close_position(action.symbol, pos, dry_run=False)
        registry.remove(action.symbol)
        if tracker and entry > 0:
            from ml.outcomes import notify_trade_close

            close_px = float(pos.get("mark_price") or 0)
            if ex.stream:
                stream_px = ex.stream.get_last_price(action.symbol)
                if stream_px and stream_px > 0:
                    close_px = float(stream_px)
            if close_px <= 0:
                close_px = entry * (1.01 if side == "long" else 0.99)
            notify_trade_close(
                tracker,
                action.symbol,
                str(side),
                entry,
                close_px,
                stop_pct=stop_pct,
                take_pct=take_pct,
                reason=action.action,
            )
        return True
    except Exception as exc:
        if _position_already_closed(exc):
            log.info(
                "rotation close %s already flat (exchange/TPSL): %s",
                action.symbol,
                exc,
            )
            registry.remove(action.symbol)
            return True
        log.exception("rotation close failed %s", action.symbol)
        return False
