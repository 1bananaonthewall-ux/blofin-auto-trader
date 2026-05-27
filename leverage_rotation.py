"""
Close open positions so the bot can re-enter at higher leverage (50x 3R).

Used on startup (one-shot) and when throughput brain detects starved + low-lev book.
Does not change steward harvest rules — only flat closes for re-entry.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from position_registry import PositionRegistry

log = logging.getLogger(__name__)

ROTATE_DONE_NAME = "leverage_rotate.done"
ROTATE_REQUEST_NAME = "leverage_rotate.request"


def _rotate_flag(state_dir: Path) -> Path:
    return state_dir / ROTATE_DONE_NAME


def close_all_for_leverage_upgrade(
    ex,
    settings,
    registry: PositionRegistry,
) -> int:
    """Market-close every open position. Returns count closed."""
    positions = ex.fetch_all_positions()
    if not positions:
        return 0
    closed = 0
    for symbol, pos in list(positions.items()):
        try:
            ex.close_position(symbol, pos, settings.dry_run)
            registry.remove(symbol)
            closed += 1
            log.warning("LEV ROTATE closed %s %s for 3R re-entry at %dx", symbol, pos.get("side"), settings.scalp_leverage_max)
            time.sleep(0.2)
        except Exception:
            log.exception("LEV ROTATE failed close %s", symbol)
    return closed


def run_startup_leverage_rotation(
    ex,
    settings,
    registry: PositionRegistry,
    state_dir: Path,
    *,
    force: bool = False,
) -> int:
    if not settings.leverage_rotate_on_start and not force:
        return 0
    flag = _rotate_flag(state_dir)
    if flag.exists() and not force:
        return 0
    n = close_all_for_leverage_upgrade(ex, settings, registry)
    if n > 0 or force:
        flag.write_text(f"rotated_at={time.time()} closed={n}\n", encoding="utf-8")
        log.warning(
            "LEV ROTATE startup: closed %d position(s) — next entries use up to %dx 3R",
            n,
            settings.scalp_leverage_max,
        )
    return n


def maybe_rotate_when_starved(
    ex,
    settings,
    registry: PositionRegistry,
    state_dir: Path,
    *,
    should_rotate: bool,
    throughput_brain,
) -> int:
    if not should_rotate or not settings.leverage_rotate_when_starved:
        return 0
    req = state_dir / ROTATE_REQUEST_NAME
    req.write_text(str(time.time()), encoding="utf-8")
    n = close_all_for_leverage_upgrade(ex, settings, registry)
    if n > 0:
        throughput_brain.mark_rotated()
        log.warning(
            "LEV ROTATE throughput: closed %d — freeing margin for %dx 3R re-entries",
            n,
            settings.scalp_leverage_max,
        )
    return n


def count_below_target_leverage(
    positions: dict,
    target_lev: int,
    registry: PositionRegistry | None = None,
    *,
    margin: int = 5,
    min_age_seconds: float = 180.0,
) -> int:
    """Count positions below target lev, ignoring fresh opens (exchange margin still settling)."""
    import time

    now = time.time()
    n = 0
    for sym, pos in positions.items():
        lev = int(pos.get("effective_leverage") or pos.get("leverage") or 0)
        if lev <= 0 or lev >= target_lev - margin:
            continue
        if registry:
            meta = registry.get(sym)
            if meta and (now - float(meta.get("opened_at", 0))) < min_age_seconds:
                continue
        n += 1
    return n
