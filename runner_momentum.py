"""Runner-momentum helpers — directional tape priority over fixed 3R scalps."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings


def runner_priority_active(settings: "Settings") -> bool:
    """When true, rank/enter directional runners with extended TP and trail."""
    if getattr(settings, "runner_priority_mode", False):
        return True
    return bool(getattr(settings, "momentum_wave_mode", False))


def is_directional_runner(decision, settings: "Settings") -> bool:
    if getattr(decision, "is_runner", False):
        return True
    run_s = float(getattr(decision, "run_score", 0.0) or 0.0)
    chop = float(getattr(decision, "chop_index", 0.5) or 0.5)
    path = float(getattr(decision, "path_efficiency", 0.5) or 0.5)
    min_run = float(getattr(settings, "runner_min_score", 0.48))
    max_chop = float(getattr(settings, "runner_max_chop", 0.56))
    min_path = float(getattr(settings, "runner_min_path_eff", 0.26))
    return (
        run_s >= min_run
        and path >= min_path
        and chop <= max_chop + 0.06
        and not getattr(decision, "is_choppy", False)
    )


def extended_runner_take_pct(
    stop_pct: float,
    *,
    settings: "Settings",
    leverage: int,
) -> float:
    """Wider TP for momentum runners (still capped)."""
    cap = float(getattr(settings, "runner_extend_take_pct", 0.08))
    min_rr = float(getattr(settings, "runner_extend_min_rr", 2.5))
    if stop_pct <= 0:
        return cap
    base = stop_pct * min_rr
    lev_gain = max(
        0.012,
        float(getattr(settings, "momentum_wave_target_levered_profit_pct", 50.0))
        / 100.0
        / max(float(leverage), 1.0),
    )
    return min(cap, max(base, lev_gain))
