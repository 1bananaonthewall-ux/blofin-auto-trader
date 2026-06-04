"""Detect deposits / larger account tier and reset micro-era state."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings

log = logging.getLogger(__name__)

_TRACK = "last_equity_track.json"


def is_micro_account(equity: float, settings: "Settings") -> bool:
    return equity > 0 and equity < getattr(settings, "micro_equity_threshold", 10.0)


def is_small_account(equity: float, settings: "Settings") -> bool:
    return equity > 0 and equity < getattr(settings, "small_account_threshold", 50.0)


def _load_track(state_dir: Path) -> dict[str, Any]:
    path = state_dir / _TRACK
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_track(state_dir: Path, equity: float) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _TRACK).write_text(
        json.dumps({"equity": round(equity, 6), "ts": time.time()}, indent=2),
        encoding="utf-8",
    )


def maybe_capital_infusion(
    engine: "AutonomousGrowthEngine",
    settings: "Settings",
    equity: float,
    *,
    ratio_threshold: float = 2.2,
) -> bool:
    """
    After a deposit (equity step-up), reset day baseline and peaks so the bot
    trades at full size instead of micro drawdown / red-day guards.
    """
    if equity <= 0:
        return False
    state_dir = settings.state_dir
    prev = float(_load_track(state_dir).get("equity") or 0)
    _save_track(state_dir, equity)

    infused = False
    if prev > 0 and equity >= prev * ratio_threshold:
        infused = True
    elif prev < 8.0 and equity >= 25.0:
        infused = True

    if not infused:
        return False

    log.warning(
        "CAPITAL INFUSION: equity $%.2f (was $%.2f) — resetting day curve + entry pauses for full-size trading",
        equity,
        prev,
    )

    try:
        from runtime_gates import clear_entries_pause

        clear_entries_pause(state_dir)
    except Exception:
        pass

    engine.manifold.reset_peaks(equity)
    engine.pnl.reset_peak(equity)

    today = time.strftime("%Y-%m-%d")
    engine.growth.history = [
        h for h in engine.growth.history if h.get("day") != today
    ]
    engine.growth.record_equity_snapshot(equity)
    engine.growth._save()

    ticks_path = state_dir / "equity_ticks.jsonl"
    if ticks_path.is_file():
        try:
            row = json.dumps({"ts": time.time(), "equity": equity, "event": "infusion"})
            with ticks_path.open("a", encoding="utf-8") as fh:
                fh.write(row + "\n")
        except OSError:
            pass

    return True
