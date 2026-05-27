"""
Position brain — compatibility shim; book maintenance lives in core_brain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from position_registry import PositionRegistry

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings
    from exchange_client import BlofinExchange
    from throughput_brain import ThroughputState


@dataclass
class BookReconcileReport:
    open_count: int
    sltp_repaired: int
    leverage_set: int
    upgraded_closed: int
    healthy: int
    under_levered: int


def reconcile_open_book(
    ex: "BlofinExchange",
    settings: "Settings",
    registry: PositionRegistry,
    engine: "AutonomousGrowthEngine",
    *,
    throughput: "ThroughputState | None" = None,
    max_closes_per_pass: int = 1,
) -> BookReconcileReport:
    """Delegate to CoreBrain.reconcile_book (throughput arg ignored)."""
    _ = throughput
    report = engine.core.reconcile_book(
        ex,
        settings,
        registry,
        max_closes=max_closes_per_pass,
    )
    return BookReconcileReport(
        open_count=report.open_count,
        sltp_repaired=report.sltp_repaired,
        leverage_set=report.leverage_set,
        upgraded_closed=report.upgraded_closed,
        healthy=report.healthy,
        under_levered=report.under_levered,
    )
