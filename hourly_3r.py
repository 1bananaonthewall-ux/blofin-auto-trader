"""
Hourly 3R winner mode — pace entries toward N closed wins/hour at fixed 3:1 TP/SL.

Uses profitability.json closes (net_pnl > 0) plus open count for starvation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings
    from scalp_optimizer import ScalpTuning


def hourly_3r_active(settings: "Settings") -> bool:
    return bool(getattr(settings, "hourly_3r_winner_mode", False))


def target_wins_per_hour(settings: "Settings") -> int:
    return max(1, int(getattr(settings, "optimizer_target_min_wins_per_hour", 3)))


def target_min_opens_per_hour(settings: "Settings") -> int:
    """Opens/hour floor — scales with win target (~2.5 opens per desired win at ~40% WR)."""
    try:
        from account_guard import universe_fill_active

        if universe_fill_active(settings):
            return 0
    except Exception:
        pass
    base = int(settings.optimizer_target_min_tph)
    if not hourly_3r_active(settings):
        return base
    wins = target_wins_per_hour(settings)
    scaled = int(wins * 2.5)
    return max(base, scaled)


def count_wins_since(state_dir: Path, since_ts: float) -> int:
    from ml.outcomes import count_outcome_wins_since

    outcome_wins = count_outcome_wins_since(state_dir, since_ts)
    path = state_dir / "profitability.json"
    pnl_wins = 0
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            trades = raw.get("trades", [])
            from scalp_optimizer import _parse_ts

            recent = [
                t
                for t in trades
                if _parse_ts(t.get("ts", t.get("closed_ts", 0))) >= since_ts
            ]
            pnl_wins = sum(1 for t in recent if float(t.get("net_pnl", 0)) > 0)
        except Exception:
            pnl_wins = 0
    return max(outcome_wins, pnl_wins)


def get_active_tuning_safe():
    from scalp_optimizer import get_active_tuning

    return get_active_tuning()


def wins_last_hour(settings: "Settings") -> int:
    t = get_active_tuning_safe()
    return int(getattr(t, "wins_last_hour", 0))


def is_wins_starved(settings: "Settings", tuning: "ScalpTuning | None" = None) -> bool:
    if not hourly_3r_active(settings):
        return False
    t = tuning or get_active_tuning_safe()
    return int(getattr(t, "wins_last_hour", 0)) < target_wins_per_hour(settings)


def is_opens_starved(settings: "Settings", tuning: "ScalpTuning | None" = None) -> bool:
    t = tuning or get_active_tuning_safe()
    need = target_min_opens_per_hour(settings)
    return t.trades_last_hour < need


def is_entry_starved(settings: "Settings", tuning: "ScalpTuning | None" = None) -> bool:
    if hourly_3r_active(settings):
        return is_wins_starved(settings, tuning) or is_opens_starved(settings, tuning)
    return is_opens_starved(settings, tuning)
