"""
Continuous stewardship of every open position — runs in a background loop
alongside the main scan/entry cycle so SL/TP, harvest, and stream priority
never wait on the slow conviction scan.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from markets import symbol_to_inst_id
from position_registry import PositionRegistry
from position_rotator import evaluate_harvest, evaluate_roe_harvest, execute_rotation
from liquidation_guard import (
    is_sl_reached,
    is_sl_trigger_hit,
    is_tp_reached,
    is_tp_trigger_hit,
    sl_target_price,
    tp_target_price,
)  # sl/tp_target_price used by _finalize_closed_trade
from ml.outcomes import label_registry_closes, notify_trade_close
from position_brain import reconcile_open_book
from dashboard_publish import publish_account_snapshot
from scalp_profile import profile_for

import api_backoff

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings
    from exchange_client import BlofinExchange
    from margin_migrator import CrossMarginAutoMigrator
    from ml.outcomes import TradeOutcomeTracker

log = logging.getLogger(__name__)


def _gross_pnl_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if side == "long":
        return (price - entry) / entry
    return (entry - price) / entry


def enrich_positions(positions: dict, registry: PositionRegistry) -> dict:
    """Merge registry SL/TP metadata into live exchange position dicts."""
    for sym, pos in positions.items():
        symbol = str(pos.get("symbol") or sym.split("#")[0])
        meta = registry.get(symbol) or registry.get(sym)
        if not meta:
            continue
        pos.setdefault("stop_pct", meta.get("stop_pct"))
        pos.setdefault("take_pct", meta.get("take_pct"))
        if not pos.get("entry_price"):
            pos["entry_price"] = meta.get("entry_price")
        if not pos.get("side"):
            pos["side"] = meta.get("side")
    return positions


def adopt_exchange_positions(
    registry: PositionRegistry,
    positions: dict,
    settings: "Settings | None" = None,
    *,
    default_leverage: int = 10,
    default_stop_pct: float = 0.012,
    default_take_pct: float = 0.022,
) -> int:
    """Register positions opened outside the bot (or after restart) so we can manage them."""
    if settings is not None and settings.scalp_3r_mode:
        default_stop_pct = settings.scalp_max_stop_pct
        default_take_pct = settings.scalp_max_stop_pct * settings.scalp_3r_min_rr
    adopted = 0
    for sym, pos in positions.items():
        symbol = str(pos.get("symbol") or sym.split("#")[0])
        if registry.get(symbol):
            continue
        entry = float(pos.get("entry_price") or 0)
        side = pos.get("side") or "long"
        if entry <= 0:
            continue
        trade_style = None
        if settings is not None:
            from tpsl_policy import fast_lethal_cross_mode

            if fast_lethal_cross_mode(settings):
                trade_style = "fast_3r"
        registry.record_open(
            symbol,
            side=side,
            entry_price=entry,
            leverage=int(pos.get("leverage") or default_leverage),
            stop_pct=default_stop_pct,
            take_pct=default_take_pct,
            conviction=0.5,
            trade_style=trade_style,
        )
        adopted += 1
        log.info("steward adopted unmanaged position %s %s @ %.4f", symbol, side, entry)
    return adopted


def _sl_ref_price(side: str, mark: float, last: float) -> float:
    """Worst-case price for SL: longs use lower; shorts use higher."""
    if mark <= 0:
        return last
    if last <= 0:
        return mark
    side_l = str(side).lower()
    if side_l == "long":
        return min(mark, last)
    return max(mark, last)


def _finalize_closed_trade(
    ex: "BlofinExchange",
    engine: "AutonomousGrowthEngine",
    tracker: Any,
    registry: PositionRegistry,
    symbol: str,
    pos: dict,
    *,
    reason: str,
    close_px_hint: float,
    stop_pct: float,
    take_pct: float,
) -> tuple[float, float]:
    """Fetch exchange fill PnL when possible; label outcome + profitability."""
    from exchange_client import BlofinExchange

    meta = registry.get(symbol) or {}
    side = str(pos.get("side") or meta.get("side") or "long")
    entry = float(pos.get("entry_price") or meta.get("entry_price") or 0)
    margin = float(meta.get("margin_usdt") or pos.get("margin_usdt") or 0)
    contracts = float(pos.get("contracts") or meta.get("contracts") or 0)
    lev = int(meta.get("leverage") or pos.get("leverage") or 0) or None
    opened_at = float(meta.get("opened_at") or 0) or None
    market = ex.market_for(symbol)
    ct_size = float(getattr(market, "contract_size", None) or 1.0)

    fill_pnl: float | None = None
    fill_ratio: float | None = None
    close_px = float(close_px_hint or 0)
    try:
        fill = ex.fetch_recent_close_fill(symbol, side, opened_at=opened_at)
    except Exception:
        fill = None
    if fill and float(fill.get("fill_price") or 0) > 0:
        close_px = float(fill["fill_price"])
        raw_pnl = fill.get("fill_pnl")
        if raw_pnl is not None and abs(float(raw_pnl)) > 1e-9:
            fill_pnl = float(raw_pnl)
        raw_ratio = fill.get("fill_pnl_ratio")
        if raw_ratio is not None:
            try:
                fill_ratio = float(raw_ratio)
            except (TypeError, ValueError):
                fill_ratio = None

    sl_px, tp_px = sl_target_price(side, entry, stop_pct), tp_target_price(side, entry, take_pct)
    notify_trade_close(
        tracker,
        symbol,
        side,
        entry,
        close_px,
        stop_pct=stop_pct,
        take_pct=take_pct,
        stop_price=sl_px,
        take_price=tp_px,
        reason=reason,
        fill_pnl=fill_pnl,
        leverage=lev,
        margin_usdt=margin if margin > 0 else None,
        contracts=contracts if contracts > 0 else None,
        fill_pnl_ratio=fill_ratio,
    )

    if fill_pnl is not None:
        pnl_usd = fill_pnl
    else:
        info = pos.get("info") or {}
        unreal = float(pos.get("unrealizedPnl") or info.get("unrealizedPnl") or 0)
        if abs(unreal) > 1e-6:
            pnl_usd = unreal
        else:
            gross = _gross_pnl_pct(side, entry, close_px)
            pnl_usd = BlofinExchange.estimate_realized_pnl_usd(
                side=side,
                entry=entry,
                exit_px=close_px,
                margin_usdt=margin if margin > 0 else None,
                leverage=lev,
                contracts=contracts if contracts > 0 else None,
                contract_size=ct_size,
                notional_usdt=float(pos.get("notional_usdt") or 0) or None,
            )
            if abs(pnl_usd) < 1e-6 and float(pos.get("notional_usdt") or 0) > 0:
                pnl_usd = float(pos["notional_usdt"]) * gross

    engine.record_closed_trade(
        symbol,
        pnl_usd,
        side=side,
        event=reason,
        entry=entry,
        exit_px=close_px,
        leverage=lev,
        margin_usdt=margin if margin > 0 else None,
        contracts=contracts if contracts > 0 else None,
    )
    return close_px, pnl_usd


def _position_already_closed(exc: BaseException) -> bool:
    """Exchange close when flat (TPSL filled first) — not a steward failure."""
    msg = str(exc).lower()
    return "102005" in msg or "had been closed" in msg or "position does not exist" in msg


def _tp_ref_price(side: str, mark: float, last: float) -> float:
    """Best-case price for TP: longs use higher; shorts use lower."""
    if mark <= 0:
        return last
    if last <= 0:
        return mark
    side_l = str(side).lower()
    if side_l == "long":
        return max(mark, last)
    return min(mark, last)


def _resolve_sl_tp_triggers(
    ex: "BlofinExchange",
    symbol: str,
    side: str,
    entry: float,
    stop_pct: float,
    take_pct: float,
    meta: dict | None,
) -> tuple[float, float]:
    """Prefer live exchange brackets, then registry absolutes, then pct targets."""
    sl_trig = sl_target_price(side, entry, stop_pct)
    tp_trig = tp_target_price(side, entry, take_pct)
    if meta:
        reg_sl = float(meta.get("sl_price") or 0)
        reg_tp = float(meta.get("tp_price") or 0)
        if reg_sl > 0:
            sl_trig = reg_sl
        if reg_tp > 0:
            tp_trig = reg_tp
    pending_fn = getattr(ex, "_pending_tpsl", None)
    if pending_fn and entry > 0:
        try:
            from markets import symbol_to_inst_id

            _, pending = pending_fn(symbol_to_inst_id(symbol), side, entry)
            if pending.has_sl and pending.sl_price > 0:
                sl_trig = pending.sl_price
            if pending.has_tp and pending.tp_price > 0:
                tp_trig = pending.tp_price
        except Exception:
            pass
    return sl_trig, tp_trig


def close_trigger_breached(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: PositionRegistry,
    engine: "AutonomousGrowthEngine",
    tracker: "TradeOutcomeTracker | None" = None,
) -> int:
    """Market-close when price crossed TP/SL but exchange TPSL did not fill."""
    closed = 0
    for symbol, pos in list(positions.items()):
        entry = float(pos.get("entry_price") or 0)
        side = pos.get("side") or "long"
        contracts = float(pos.get("contracts") or 0)
        if entry <= 0 or contracts <= 0:
            continue
        meta = registry.get(symbol)
        take_pct = float(pos.get("take_pct") or (meta or {}).get("take_pct") or 0.022)
        stop_pct = float(pos.get("stop_pct") or (meta or {}).get("stop_pct") or 0.012)
        mark = float(pos.get("mark_price") or 0)
        last = mark
        if ex.stream:
            stream_px = ex.stream.get_last_price(symbol)
            if stream_px and stream_px > 0:
                last = float(stream_px)
        sl_ref = _sl_ref_price(side, mark, last)
        tp_ref = _tp_ref_price(side, mark, last)
        if sl_ref <= 0 and tp_ref <= 0:
            continue

        sl_trig, tp_trig = _resolve_sl_tp_triggers(
            ex, symbol, side, entry, stop_pct, take_pct, meta
        )
        hit_tp = is_tp_trigger_hit(side, tp_trig, tp_ref) or is_tp_reached(
            side, entry, take_pct, tp_ref
        )
        hit_sl = is_sl_trigger_hit(side, sl_trig, sl_ref) or is_sl_reached(
            side, entry, stop_pct, sl_ref
        )
        # Exchange mark is authoritative when stream last is stale.
        if mark > 0 and not hit_sl:
            hit_sl = is_sl_trigger_hit(side, sl_trig, mark) or is_sl_reached(
                side, entry, stop_pct, mark
            )
        if not hit_tp and not hit_sl:
            continue

        reason = "tp_backup_close" if hit_tp else "sl_backup_close"
        trig_px = tp_trig if hit_tp else sl_trig
        ref = tp_ref if hit_tp else sl_ref
        gross = _gross_pnl_pct(side, entry, ref)
        log.warning(
            "%s %s %s mark=%.6f last=%.6f ref=%.6f trig=%.6f gross=%.2f%% — exchange TPSL missed, market close",
            "TP breach" if hit_tp else "SL breach",
            symbol,
            side,
            mark,
            last,
            ref,
            trig_px,
            gross * 100,
        )
        if settings.dry_run:
            notify_trade_close(
                tracker, symbol, str(side), entry, ref,
                stop_pct=stop_pct, take_pct=take_pct, reason=reason,
            )
            registry.remove(symbol)
            positions.pop(symbol, None)
            closed += 1
            continue
        try:
            ex.close_position(symbol, pos, dry_run=False)
            registry.remove(symbol)
            _finalize_closed_trade(
                ex,
                engine,
                tracker,
                registry,
                symbol,
                pos,
                reason=reason,
                close_px_hint=ref,
                stop_pct=stop_pct,
                take_pct=take_pct,
            )
            positions.pop(symbol, None)
            closed += 1
        except Exception as e:
            if _position_already_closed(e):
                log.info(
                    "trigger backup close %s — already flat (exchange TPSL likely filled)",
                    symbol,
                )
                registry.remove(symbol)
                _finalize_closed_trade(
                    ex,
                    engine,
                    tracker,
                    registry,
                    symbol,
                    pos,
                    reason=f"{reason}_already_flat",
                    close_px_hint=ref,
                    stop_pct=stop_pct,
                    take_pct=take_pct,
                )
                positions.pop(symbol, None)
                closed += 1
            else:
                log.exception("trigger backup close failed %s", symbol)
    return closed


def ensure_sltp_on_all(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    engine: "AutonomousGrowthEngine",
    registry: PositionRegistry,
) -> int:
    d = engine.doctrine
    if not d.maintain_sltp_on_open_positions:
        return 0
    fixed = 0
    for symbol, pos in positions.items():
        side = pos.get("side")
        contracts = float(pos.get("contracts") or 0)
        entry = float(pos.get("entry_price") or 0)
        if not side or contracts <= 0 or entry <= 0:
            continue
        trade_sym = str(pos.get("symbol") or symbol).split("#", 1)[0]
        meta = registry.get(trade_sym) or registry.get(symbol)
        take_pct = float(pos.get("take_pct") or (meta or {}).get("take_pct") or 0.022)
        lev = int(
            pos.get("effective_leverage")
            or (meta or {}).get("leverage")
            or (settings.scalp_leverage if settings.scalp_mode else settings.leverage)
        )
        try:
            from markets import symbol_to_inst_id
            from tpsl_guard import pending_exceeds_policy_caps, pending_is_adequate
            from tpsl_policy import resolve_tpsl_policy

            inst = symbol_to_inst_id(trade_sym)
            ps = ex._position_side_for_order(str(side), pos)
            _, pending = ex._pending_tpsl(
                inst,
                str(side),
                entry,
                position_side=ps,
                allow_registry_fallback=False,
                retries=3,
            )
            policy = resolve_tpsl_policy(
                settings, registry_meta=meta, leverage=lev
            )
            if (
                pending.live_rows > 0
                and pending_is_adequate(str(side), entry, pending)
                and not pending_exceeds_policy_caps(
                    str(side),
                    entry,
                    pending,
                    policy.max_stop_pct,
                    policy.max_take_pct,
                )
            ):
                log.debug(
                    "TPSL check %s %s — live on exchange (sl=%.6f tp=%.6f)",
                    trade_sym.split("/")[0],
                    side,
                    pending.sl_price,
                    pending.tp_price,
                )
                continue
            log.warning(
                "TPSL check %s %s — missing on exchange, repairing",
                trade_sym.split("/")[0],
                side,
            )
            ex._clear_tpsl_trust(trade_sym)
            ex._tpsl_repair_at.pop(ex._canonical_symbol(trade_sym), None)
            ok, rep_stop, rep_take = ex.repair_position_tpsl(
                trade_sym,
                str(side),
                contracts,
                take_pct=take_pct,
                configured_leverage=lev,
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
                pos["stop_pct"] = rep_stop
                pos["take_pct"] = rep_take
                fixed += 1
                log.info(
                    "TPSL repaired %s %s stop=%.2f%% take=%.2f%% (steward)",
                    trade_sym.split("/")[0],
                    side,
                    rep_stop * 100,
                    rep_take * 100,
                )
            elif not ok:
                log.warning(
                    "TPSL repair failed %s %s — will retry next steward pass",
                    trade_sym.split("/")[0],
                    side,
                )
        except Exception:
            log.exception("steward SL/TP failed %s", trade_sym)
    return fixed


def harvest_all_mature(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: PositionRegistry,
    tracker: "TradeOutcomeTracker | None",
    engine: "AutonomousGrowthEngine",
    harvest_eagerness: float,
) -> int:
    closed = 0
    for sym in list(positions.keys()):
        pos = positions.get(sym)
        market = ex.market_for(sym)
        if not pos or not market:
            continue
        last = (ex.stream.get_last_price(sym) if ex.stream else None) or float(
            pos.get("entry_price") or 0
        )
        min_hold = settings.scalp_min_hold_seconds if settings.scalp_mode else 90.0
        fee_mult = settings.scalp_harvest_fee_mult if settings.scalp_mode else 2.2
        prof = profile_for(settings)
        harvest_min_r = prof.harvest_min_r if prof else 0.0
        skip_discretionary = getattr(settings, "stack_winners_mode", True) and not getattr(
            settings, "early_harvest_enabled", False
        )
        action = None
        if getattr(settings, "scalp_roe_harvest_enabled", False):
            action = evaluate_roe_harvest(
                sym,
                pos,
                registry.get(sym),
                last,
                market,
                fee_taker=settings.fee_est_taker_pct,
                fee_maker=settings.fee_est_maker_pct,
                default_leverage=settings.scalp_leverage if settings.scalp_mode else settings.leverage,
                min_roe_pct=float(getattr(settings, "scalp_roe_harvest_min_pct", 50.0)),
                max_roe_pct=float(getattr(settings, "scalp_roe_harvest_max_pct", 60.0)),
                min_hold_seconds=min_hold,
            )
        if not action and not skip_discretionary:
            action = evaluate_harvest(
                sym,
                pos,
                registry.get(sym),
                last,
                market,
                fee_taker=settings.fee_est_taker_pct,
                fee_maker=settings.fee_est_maker_pct,
                default_leverage=settings.scalp_leverage if settings.scalp_mode else settings.leverage,
                harvest_eagerness=harvest_eagerness,
                min_hold_seconds=min_hold,
                fee_coverage_mult=fee_mult,
                harvest_min_r=harvest_min_r,
                stack_winners=getattr(settings, "stack_winners_mode", True),
                early_harvest=getattr(settings, "early_harvest_enabled", False),
            )
        if not action:
            continue
        meta = registry.get(sym) or {}
        if execute_rotation(ex, action, positions, registry, settings.dry_run, tracker):
            engine.record_closed_trade(
                sym,
                action.pnl_after_fees_usd,
                side=str(meta.get("side") or pos.get("side") or ""),
                event=action.action,
            )
            positions.pop(sym, None)
            closed += 1
            log.info("steward harvested %s: %s", sym, action.reason)
    return closed


def top_up_under_margined_positions(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
) -> int:
    """Add isolated margin on open positions below target_margin_rate."""
    from margin_mode import is_cross_margin

    if settings.dry_run or not positions or is_cross_margin(settings.margin_mode):
        return 0
    target = float(getattr(settings, "target_margin_rate", 1.15) or 1.15)
    topped = 0
    for symbol, pos in positions.items():
        trade_sym = str(pos.get("symbol") or symbol).split("#", 1)[0]
        side = str(pos.get("side") or "")
        if not side:
            continue
        mrate = float(pos.get("margin_rate") or 0)
        if mrate >= target - 0.02:
            continue
        if ex.ensure_margin_cushion(trade_sym, side, target_margin_rate=target, dry_run=False):
            topped += 1
    return topped


def close_if_near_liquidation(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: PositionRegistry,
    engine: "AutonomousGrowthEngine",
    tracker: "TradeOutcomeTracker | None",
) -> int:
    """Market-close before exchange liquidation when price consumes most of the liq buffer."""
    closed = 0
    for symbol, pos in list(positions.items()):
        entry = float(pos.get("entry_price") or 0)
        side = str(pos.get("side") or "")
        if entry <= 0 or not side:
            continue
        trade_sym = str(pos.get("symbol") or symbol).split("#", 1)[0]
        last = (ex.stream.get_last_price(trade_sym) if ex.stream else None) or float(
            pos.get("mark_price") or entry
        )
        lev = int(
            pos.get("effective_leverage")
            or pos.get("leverage")
            or settings.scalp_leverage_max
        )
        prox = ex.check_liquidation_proximity(trade_sym, side, entry, last, leverage=lev)
        if not prox.get("exit_early"):
            continue
        rem = float(prox.get("remaining_pct") or 1.0)
        log.warning(
            "pre-liq exit %s %s — %.0f%% of entry→liq buffer left (mark=%.6f liq=%.6f)",
            trade_sym.split("/")[0],
            side,
            rem * 100,
            last,
            float(prox.get("liquidation_price") or 0),
        )
        if settings.dry_run:
            registry.remove(trade_sym)
            positions.pop(symbol, None)
            closed += 1
            continue
        try:
            ex.close_position(trade_sym, pos, dry_run=False)
            registry.remove(trade_sym)
            notify_trade_close(
                tracker,
                trade_sym,
                side,
                entry,
                last,
                reason="pre_liquidation_exit",
            )
            gross = _gross_pnl_pct(side, entry, last)
            pnl_usd = float(pos.get("unrealized_pnl_usd") or 0)
            if pnl_usd == 0:
                pnl_usd = float(pos.get("notional_usdt") or 0) * gross
            engine.record_closed_trade(trade_sym, pnl_usd, side=side, event="pre_liquidation_exit")
            positions.pop(symbol, None)
            closed += 1
        except Exception:
            log.exception("pre-liq close failed %s", trade_sym)
    return closed


def prioritize_open_in_stream(ex: "BlofinExchange", positions: dict) -> None:
    if not ex.stream or not positions:
        return
    inst_ids = [symbol_to_inst_id(s) for s in positions.keys()]
    ex.stream.set_priority(inst_ids)
    for sym in list(positions.keys())[:min(30, len(positions))]:
        ex.stream.bootstrap_candles(sym, "1m", 80)
        ex.stream.bootstrap_candles(sym, "5m", 50)


def _cached_positions(ex: "BlofinExchange") -> dict:
    return dict(getattr(ex, "_cached_positions", {}) or {})


def _publish_cached_snapshot(
    ex: "BlofinExchange",
    settings: "Settings",
    registry: PositionRegistry,
) -> None:
    publish_account_snapshot(
        settings.state_dir,
        getattr(ex, "_cached_equity", 0.0),
        getattr(ex, "_cached_free", 0.0),
        _cached_positions(ex),
        registry,
        api_ok=False,
    )


def manage_all_open_positions(
    ex: "BlofinExchange",
    settings: "Settings",
    engine: "AutonomousGrowthEngine",
    registry: PositionRegistry,
    tracker: "TradeOutcomeTracker | None",
    *,
    harvest_eagerness: float = 1.0,
    cross_migrator: "CrossMarginAutoMigrator | None" = None,
) -> dict:
    """
    Full pass on every live position: sync registry, SL/TP, harvest, stream priority.
    Returns current open positions dict (may be smaller after harvest).
    """
    if api_backoff.is_paused():
        positions = enrich_positions(_cached_positions(ex), registry)
        _publish_cached_snapshot(ex, settings, registry)
        return positions

    positions = ex.fetch_all_positions()
    pos_ok = ex.positions_fetch_ok
    open_syms = {str(p.get("symbol") or k.split("#")[0]) for k, p in positions.items()}
    if pos_ok:
        label_registry_closes(registry, open_syms, ex, tracker, engine=engine)
    adopt_exchange_positions(
        registry,
        positions,
        settings,
        default_leverage=settings.leverage,
        default_stop_pct=engine.doctrine.min_take_profit_pct * 3,
        default_take_pct=max(engine.doctrine.min_take_profit_pct * 5, 0.022),
    )
    positions = enrich_positions(positions, registry)
    if cross_migrator is not None:
        migrated = cross_migrator.run(ex, registry, positions, max_per_pass=1)
        if migrated:
            positions = ex.fetch_all_positions()
            open_syms = {str(p.get("symbol") or k.split("#")[0]) for k, p in positions.items()}
            if pos_ok:
                label_registry_closes(registry, open_syms, ex, tracker, engine=engine)
            positions = enrich_positions(positions, registry)
    prioritize_open_in_stream(ex, positions)
    from tpsl_policy import fast_lethal_cross_mode

    pre_liq = 0
    if not fast_lethal_cross_mode(settings):
        pre_liq = close_if_near_liquidation(ex, settings, positions, registry, engine, tracker)
    if pre_liq:
        positions = ex.fetch_all_positions()
        positions = enrich_positions(positions, registry)
    top_up_under_margined_positions(ex, settings, positions)
    # SL/TP backup close before slow reconcile — do not wait on TPSL repair loop.
    tp_closed = close_trigger_breached(ex, settings, positions, registry, engine, tracker)
    if tp_closed:
        positions = ex.fetch_all_positions()
        open_syms = {str(p.get("symbol") or k.split("#")[0]) for k, p in positions.items()}
        label_registry_closes(registry, open_syms, ex, tracker, engine=engine)
        positions = enrich_positions(positions, registry)
    tp = getattr(engine, "_last_throughput", None)
    max_book_closes = 1 if settings.leverage_auto_upgrade else 0
    book = reconcile_open_book(
        ex,
        settings,
        registry,
        engine,
        throughput=tp,
        max_closes_per_pass=max_book_closes,
        tracker=tracker,
    )
    tpsl_attached = ensure_sltp_on_all(ex, settings, positions, engine, registry)
    exchange_live = 0
    for _sym, _pos in positions.items():
        _trade = str(_pos.get("symbol") or _sym).split("#", 1)[0]
        _side = str(_pos.get("side") or "")
        _entry = float(_pos.get("entry_price") or 0)
        if _entry > 0 and ex.live_exchange_tpsl(_trade, _side, _entry, pos=_pos):
            exchange_live += 1
    # Re-check after reconcile in case price moved while repairing other symbols.
    if close_trigger_breached(ex, settings, positions, registry, engine, tracker):
        positions = ex.fetch_all_positions()
        positions = enrich_positions(positions, registry)
    harvested = harvest_all_mature(
        ex, settings, positions, registry, tracker, engine, harvest_eagerness
    )
    try:
        from llm_exit_advisor import maybe_advise_exits

        qwen_closed = maybe_advise_exits(
            ex,
            settings,
            positions,
            registry,
            engine,
            tracker,
            harvest_eagerness=harvest_eagerness,
        )
        if qwen_closed:
            positions = ex.fetch_all_positions()
            positions = enrich_positions(positions, registry)
            harvested += qwen_closed
    except Exception:
        log.debug("qwen exit advisor failed", exc_info=True)

    if positions:
        lines = []
        for sym, pos in positions.items():
            entry = float(pos.get("entry_price") or 0)
            side = pos.get("side") or "long"
            last = (ex.stream.get_last_price(sym) if ex.stream else None) or entry
            gross = _gross_pnl_pct(side, entry, last)
            take_pct = float(pos.get("take_pct") or 0.022)
            tp_px = tp_target_price(side, entry, take_pct)
            at_tp = " @TP" if is_tp_reached(side, entry, take_pct, last) else ""
            lines.append(
                f"{sym.split('/')[0]} {gross:+.2%}/{take_pct:.2%}{at_tp} tp={tp_px:.5f}"
            )
        log.info(
            "steward %d open | exchange TP/SL live=%d/%d attached=%d | harvested %d | %s",
            len(positions),
            exchange_live,
            len(positions),
            tpsl_attached,
            harvested,
            " | ".join(lines),
        )
        if exchange_live < len(positions):
            log.warning(
                "steward: %d/%d positions missing exchange TP/SL — restart bot if this persists",
                len(positions) - exchange_live,
                len(positions),
            )
    publish_account_snapshot(
        settings.state_dir,
        ex.fetch_equity_usdt(),
        ex.fetch_free_equity_usdt(),
        positions,
        registry,
        api_ok=pos_ok and ex.equity_fetch_ok,
    )
    return positions


def steward_interval_seconds(open_count: int, *, scalp: bool = False, scalp_interval: float = 4.0) -> float:
    if open_count <= 0:
        return 18.0 if scalp else 25.0
    if scalp:
        return max(2.0, min(6.0, scalp_interval * 0.75))
    return max(5.0, min(12.0, 14.0 - open_count * 0.25))


class PositionSteward:
    """Background loop — manages all trades continuously."""

    def __init__(
        self,
        ex: "BlofinExchange",
        settings: "Settings",
        engine: "AutonomousGrowthEngine",
        registry: PositionRegistry,
        tracker: "TradeOutcomeTracker | None",
        harvest_eagerness_fn: Callable[[], float] | None = None,
        cross_migrator: "CrossMarginAutoMigrator | None" = None,
    ) -> None:
        self.ex = ex
        self.settings = settings
        self.engine = engine
        self.registry = registry
        self.tracker = tracker
        self.cross_migrator = cross_migrator
        self._harvest_fn = harvest_eagerness_fn or (lambda: 1.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="position-steward", daemon=True)
        self._thread.start()
        log.info("position steward started — all open trades managed continuously")

    def stop(self) -> None:
        self._stop.set()

    def run_once_now(self) -> dict:
        with self._lock:
            return manage_all_open_positions(
                self.ex,
                self.settings,
                self.engine,
                self.registry,
                self.tracker,
                harvest_eagerness=self._harvest_fn(),
                cross_migrator=self.cross_migrator,
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            n = 0
            try:
                if api_backoff.is_paused():
                    positions = _cached_positions(self.ex)
                    n = len(positions)
                    eq = self.ex._cached_equity
                    fm = self.ex._cached_free
                    if n > 0 and eq > 0:
                        self.engine.update_fluid(eq, fm, n)
                    _publish_cached_snapshot(self.ex, self.settings, self.registry)
                    wait = max(60.0, api_backoff.seconds_left())
                    self._stop.wait(wait)
                    continue
                with self._lock:
                    positions = self.ex.fetch_all_positions()
                    n = len(positions)
                    if n > 0:
                        eq = self.ex.fetch_equity_usdt()
                        fm = self.ex.fetch_free_equity_usdt()
                        self.engine.update_fluid(eq, fm, n)
                        manage_all_open_positions(
                            self.ex,
                            self.settings,
                            self.engine,
                            self.registry,
                            self.tracker,
                            harvest_eagerness=self._harvest_fn(),
                            cross_migrator=self.cross_migrator,
                        )
            except PermissionError as exc:
                log.warning("position steward cycle: snapshot write denied (%s)", exc)
            except Exception:
                log.exception("position steward cycle failed")
            self._stop.wait(
                steward_interval_seconds(
                    n,
                    scalp=self.settings.scalp_mode,
                    scalp_interval=self.settings.scalp_steward_interval,
                )
            )
