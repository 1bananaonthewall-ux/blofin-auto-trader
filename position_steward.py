"""
Continuous stewardship of every open position — runs in a background loop
alongside the main scan/entry cycle so SL/TP, harvest, and stream priority
never wait on the slow conviction scan.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

from markets import symbol_to_inst_id
from position_registry import PositionRegistry
from position_rotator import evaluate_harvest, execute_rotation
from position_brain import reconcile_open_book
from scalp_profile import profile_for

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings
    from exchange_client import BlofinExchange
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
        meta = registry.get(sym)
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
        if registry.get(sym):
            continue
        entry = float(pos.get("entry_price") or 0)
        side = pos.get("side") or "long"
        if entry <= 0:
            continue
        registry.record_open(
            sym,
            side=side,
            entry_price=entry,
            leverage=default_leverage,
            stop_pct=default_stop_pct,
            take_pct=default_take_pct,
            conviction=0.5,
        )
        adopted += 1
        log.info("steward adopted unmanaged position %s %s @ %.4f", sym, side, entry)
    return adopted


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
        meta = registry.get(symbol)
        take_pct = float(pos.get("take_pct") or (meta or {}).get("take_pct") or 0.022)
        lev = int(
            pos.get("effective_leverage")
            or (meta or {}).get("leverage")
            or (settings.scalp_leverage if settings.scalp_mode else settings.leverage)
        )
        try:
            ok, rep_stop, rep_take = ex.repair_position_tpsl(
                symbol,
                side,
                contracts,
                take_pct=take_pct,
                configured_leverage=lev,
                dry_run=settings.dry_run,
                cancel_existing=True,
            )
            if ok and rep_stop > 0 and rep_take > 0:
                registry.update_tpsl(symbol, stop_pct=rep_stop, take_pct=rep_take)
                pos["stop_pct"] = rep_stop
                pos["take_pct"] = rep_take
                fixed += 1
        except Exception:
            log.exception("steward SL/TP failed %s", symbol)
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


def prioritize_open_in_stream(ex: "BlofinExchange", positions: dict) -> None:
    if not ex.stream or not positions:
        return
    inst_ids = [symbol_to_inst_id(s) for s in positions.keys()]
    ex.stream.set_priority(inst_ids)
    for sym in list(positions.keys())[:min(30, len(positions))]:
        ex.stream.bootstrap_candles(sym, "1m", 80)
        ex.stream.bootstrap_candles(sym, "5m", 50)


def manage_all_open_positions(
    ex: "BlofinExchange",
    settings: "Settings",
    engine: "AutonomousGrowthEngine",
    registry: PositionRegistry,
    tracker: "TradeOutcomeTracker | None",
    *,
    harvest_eagerness: float = 1.0,
) -> dict:
    """
    Full pass on every live position: sync registry, SL/TP, harvest, stream priority.
    Returns current open positions dict (may be smaller after harvest).
    """
    positions = ex.fetch_all_positions()
    registry.sync_with_exchange(set(positions.keys()))
    adopt_exchange_positions(
        registry,
        positions,
        settings,
        default_leverage=settings.leverage,
        default_stop_pct=engine.doctrine.min_take_profit_pct * 3,
        default_take_pct=max(engine.doctrine.min_take_profit_pct * 5, 0.022),
    )
    positions = enrich_positions(positions, registry)
    prioritize_open_in_stream(ex, positions)
    tp = getattr(engine, "_last_throughput", None)
    book = reconcile_open_book(
        ex, settings, registry, engine, throughput=tp, max_closes_per_pass=1
    )
    sltp_n = book.sltp_repaired
    harvested = harvest_all_mature(
        ex, settings, positions, registry, tracker, engine, harvest_eagerness
    )

    if positions:
        lines = []
        for sym, pos in positions.items():
            entry = float(pos.get("entry_price") or 0)
            side = pos.get("side") or "long"
            last = (ex.stream.get_last_price(sym) if ex.stream else None) or entry
            gross = _gross_pnl_pct(side, entry, last)
            lines.append(f"{sym.split('/')[0]} {gross:+.2%}")
        log.info(
            "steward %d open | SL/TP checked %d | harvested %d | %s",
            len(positions),
            sltp_n,
            harvested,
            " | ".join(lines),
        )
    return positions


def steward_interval_seconds(open_count: int, *, scalp: bool = False, scalp_interval: float = 4.0) -> float:
    if open_count <= 0:
        return 18.0 if scalp else 25.0
    if scalp:
        return max(3.0, min(8.0, scalp_interval))
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
    ) -> None:
        self.ex = ex
        self.settings = settings
        self.engine = engine
        self.registry = registry
        self.tracker = tracker
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
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            n = 0
            try:
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
                        )
            except Exception:
                log.exception("position steward cycle failed")
            self._stop.wait(
                steward_interval_seconds(
                    n,
                    scalp=self.settings.scalp_mode,
                    scalp_interval=self.settings.scalp_steward_interval,
                )
            )
