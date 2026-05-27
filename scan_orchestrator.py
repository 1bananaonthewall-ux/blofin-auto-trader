"""
Adaptive scan depth — sweeps the full exchange universe with a budget that
rises and falls with stream health, fluid intensity, and PnL curve state.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from markets import symbol_to_inst_id

if TYPE_CHECKING:
    from market_stream import BlofinMarketStream

log = logging.getLogger(__name__)


@dataclass
class ScanPlan:
    depth: int
    momentum_slots: int
    rotation_slots: int
    universe_n: int
    rotation_offset: int
    stream_fresh: bool
    ticker_coverage: float


class ScanOrchestrator:
    """Rotates through every tradeable symbol over multiple ticks."""

    def __init__(self) -> None:
        self._rotation_offset = 0

    def compute_depth(
        self,
        universe_n: int,
        knobs,
        stream_health: dict | None,
        *,
        open_count: int = 0,
    ) -> int:
        if universe_n <= 0:
            return 0

        ai = knobs.action_intensity
        rel = knobs.path_reliability
        health = stream_health or {}

        # Full-universe sweep: more intensity -> cover all assets in fewer ticks
        cycles_to_cover = _blend(28.0, 5.0, ai * rel)
        if getattr(knobs, "preserve_capital", False):
            cycles_to_cover *= 1.5
        if getattr(knobs, "curve_phase", "") in ("flat", "declining"):
            cycles_to_cover *= 1.35
        elif getattr(knobs, "curve_phase", "") == "vertical":
            cycles_to_cover *= 0.85

        rotation_depth = int(math.ceil(universe_n / max(3.0, cycles_to_cover)))
        depth = max(int(knobs.symbols_per_tick), rotation_depth)

        age = float(health.get("ticker_age_sec", 30.0))
        cov = float(health.get("ticker_coverage", 0.5))
        if age < 14:
            depth = int(depth * 1.18)
        elif age > 28:
            depth = int(depth * 0.72)
        depth = int(depth * (0.65 + 0.35 * cov))
        if health.get("ws_live"):
            depth = int(depth * 1.06)

        depth = int(depth * max(0.5, 1.0 - open_count / 45.0))

        floor = max(25, min(80, universe_n // 8))
        return max(floor, min(universe_n, depth))

    def build_plan(
        self,
        all_symbols: list[str],
        held: set[str],
        stream: "BlofinMarketStream | None",
        knobs,
        *,
        open_count: int = 0,
    ) -> ScanPlan:
        universe_n = len(all_symbols)
        health = stream.stream_health() if stream else None
        depth = self.compute_depth(universe_n, knobs, health, open_count=open_count)
        held_n = len(held)
        budget = depth + held_n
        momentum_slots = max(0, int((budget - held_n) * 0.72))
        rotation_slots = max(0, budget - held_n - momentum_slots)

        fresh = bool(health and health.get("ticker_age_sec", 99) < 16)
        cov = float(health.get("ticker_coverage", 0) if health else 0)

        return ScanPlan(
            depth=depth,
            momentum_slots=momentum_slots,
            rotation_slots=rotation_slots,
            universe_n=universe_n,
            rotation_offset=self._rotation_offset,
            stream_fresh=fresh,
            ticker_coverage=cov,
        )

    def pick_symbols(
        self,
        all_symbols: list[str],
        held: set[str],
        stream: "BlofinMarketStream | None",
        knobs,
        *,
        open_count: int = 0,
    ) -> tuple[list[str], ScanPlan]:
        plan = self.build_plan(all_symbols, held, stream, knobs, open_count=open_count)
        scan = list(held)
        remaining = plan.depth

        pool = [s for s in all_symbols if s not in held]
        if not pool:
            return scan, plan

        inst_ids = [symbol_to_inst_id(s) for s in pool]

        if stream and plan.momentum_slots > 0:
            ranked = stream.momentum_rank(inst_ids, top_n=plan.momentum_slots)
            for s in ranked:
                if remaining <= 0:
                    break
                if s not in scan:
                    scan.append(s)
                    remaining -= 1

        if remaining > 0 and plan.rotation_slots > 0:
            n = len(pool)
            start = self._rotation_offset % n
            added = 0
            i = 0
            while added < remaining and i < n:
                s = pool[(start + i) % n]
                if s not in scan:
                    scan.append(s)
                    added += 1
                i += 1
            self._rotation_offset = (start + max(added, 1)) % n

        return scan, plan

    def advance_ws_rotation(self, universe_inst_ids: list[str], depth: int) -> int:
        """Cursor for rotating WS ticker subscriptions across the universe."""
        if not universe_inst_ids:
            return 0
        batch = max(35, min(120, depth // 4))
        start = self._rotation_offset % len(universe_inst_ids)
        return start


def _blend(lo: float, hi: float, t: float) -> float:
    t = max(0.0, min(1.0, t))
    return lo + (hi - lo) * t
