"""
Throughput brain — mission-aligned pacing for 3–12 quality 3R opens/hour at max leverage.

Drives entry gap, leverage target, apex fallback, and optional full-book rotation
when the bot is starved but capital is tied in lower-leverage positions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings


@dataclass(frozen=True)
class ThroughputState:
    opens_last_hour: int
    starved: bool
    overheating: bool
    target_entry_gap: float
    target_leverage: int
    allow_elite_fallback: bool
    should_rotate_leverage: bool
    should_repair_book: bool
    directive: str


class ThroughputBrain:
    def __init__(self) -> None:
        self._last_rotate_ts = 0.0
        self._last: ThroughputState | None = None

    @property
    def last_state(self) -> ThroughputState | None:
        return self._last

    def evaluate(
        self,
        settings: "Settings",
        *,
        opens_last_hour: int,
        open_count: int,
        equity: float,
        free_margin: float,
        low_leverage_positions: int = 0,
    ) -> ThroughputState:
        tmin = settings.optimizer_target_min_tph
        tmax = settings.optimizer_target_max_tph
        starved = opens_last_hour < tmin
        overheating = opens_last_hour > tmax

        gap = float(settings.scalp_entry_gap_seconds)
        if starved:
            gap = max(8.0, gap - 6.0)
        elif overheating:
            gap = min(45.0, gap + 8.0)

        target_lev = int(settings.scalp_leverage_max)

        rotate = False
        if (
            settings.leverage_rotate_when_starved
            and not getattr(settings, "leverage_auto_upgrade", True)
            and starved
            and open_count > 0
            and low_leverage_positions > 0
            and (time.time() - self._last_rotate_ts) > settings.leverage_rotate_interval_minutes * 60
        ):
            rotate = True

        if starved:
            directive = (
                f"THROUGHPUT STARVED {opens_last_hour}/{tmin} tph — "
                f"gap={gap:.0f}s target_lev={target_lev}x 3R"
            )
        elif overheating:
            directive = f"THROUGHPUT HOT {opens_last_hour}>{tmax}/hr — widen gap"
        else:
            directive = f"THROUGHPUT ON PACE {opens_last_hour} tph — {target_lev}x 3R"

        state = ThroughputState(
            opens_last_hour=opens_last_hour,
            starved=starved,
            overheating=overheating,
            target_entry_gap=gap,
            target_leverage=target_lev,
            allow_elite_fallback=starved or not settings.winner_apex_preferred,
            should_rotate_leverage=rotate,
            should_repair_book=open_count > 0,
            directive=directive,
        )
        self._last = state
        return state

    def mark_rotated(self) -> None:
        self._last_rotate_ts = time.time()
