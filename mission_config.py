"""
Single source of truth for the engine's only objective.
"""

from __future__ import annotations

# Sole target — every subsystem imports from here
SOLE_OBJECTIVE = "Maintain and exceed 10% account growth per day"
TARGET_DAILY_GROWTH_PCT = 10.0
PACE_REFERENCE_DAILY_PCT = TARGET_DAILY_GROWTH_PCT
TARGET_DAILY_GROWTH_MULT = 1.0 + TARGET_DAILY_GROWTH_PCT / 100.0
START_CAPITAL_REFERENCE = 100.0

# Floor for "maintain" (9.5% still counts as on pace at 10% goal)
DAILY_GROWTH_ON_TRACK_RATIO = 0.95


def target_daily_growth_pct() -> float:
    return TARGET_DAILY_GROWTH_PCT


def target_daily_growth_multiplier() -> float:
    return TARGET_DAILY_GROWTH_MULT


def sole_objective_label() -> str:
    return SOLE_OBJECTIVE


def daily_growth_on_track(actual_daily_pct: float) -> bool:
    """True when today's return meets or exceeds the 10%/day mission floor."""
    return actual_daily_pct >= TARGET_DAILY_GROWTH_PCT * DAILY_GROWTH_ON_TRACK_RATIO


def progress_toward_daily_goal_pct(actual_daily_pct: float) -> float:
    """0–100+ : progress vs 10%/day (exceeding 10% scores above 100%)."""
    if TARGET_DAILY_GROWTH_PCT <= 0:
        return 0.0
    return max(0.0, (actual_daily_pct / TARGET_DAILY_GROWTH_PCT) * 100.0)


def daily_growth_shortfall_pct(actual_daily_pct: float) -> float:
    """Gap below the 10%/day maintain floor (0 when exceeding)."""
    return max(0.0, TARGET_DAILY_GROWTH_PCT - actual_daily_pct)


def progress_acceleration_pct(actual_daily_pct: float, aggression_boost: float = 1.0) -> float:
    """Backward-compatible alias."""
    _ = aggression_boost
    return progress_toward_daily_goal_pct(actual_daily_pct)


def daily_growth_on_track_legacy(actual_daily_pct: float, *, aggression_boost: float = 1.0) -> bool:
    _ = aggression_boost
    return daily_growth_on_track(actual_daily_pct)


def growth_acceleration_on_track(actual_daily_pct: float, *, aggression_boost: float = 1.0) -> bool:
    _ = aggression_boost
    return daily_growth_on_track(actual_daily_pct)


def acceleration_shortfall_pct(actual_daily_pct: float, aggression_boost: float = 1.0) -> float:
    _ = aggression_boost
    return daily_growth_shortfall_pct(actual_daily_pct)
