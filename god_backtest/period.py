"""Backtest date ranges — request up to 10y; actual depth is per-symbol from Blofin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

REQUESTED_MAX_DAYS = 3650  # ~10 years (API usually returns far less per listing)
MIN_LOOKBACK_DAYS = 14
MS_PER_DAY = 86_400_000


def _parse_date_utc(date_str: str) -> datetime:
    return datetime.strptime(date_str.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def resolve_god_backtest_range(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    if start_date and end_date:
        start_dt = _parse_date_utc(start_date)
        end_dt = _parse_date_utc(end_date).replace(hour=23, minute=59, second=59, microsecond=999000)
        if end_dt > now:
            end_dt = now
        if start_dt >= end_dt:
            raise ValueError("start_date must be before end_date")
        span = (end_dt.date() - start_dt.date()).days
        if span < MIN_LOOKBACK_DAYS:
            raise ValueError(f"Date range must be at least {MIN_LOOKBACK_DAYS} days")
        return {
            "start_ms": int(start_dt.timestamp() * 1000),
            "end_ms": int(end_dt.timestamp() * 1000),
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "lookback_days": span,
            "requested_max_days": REQUESTED_MAX_DAYS,
        }

    days = max(MIN_LOOKBACK_DAYS, min(REQUESTED_MAX_DAYS, int(lookback_days or 730)))
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - days * MS_PER_DAY
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": now.strftime("%Y-%m-%d"),
        "lookback_days": days,
        "requested_max_days": REQUESTED_MAX_DAYS,
    }


def fold_windows(
    start_ms: int,
    end_ms: int,
    *,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
) -> list[dict[str, int]]:
    """Rolling walk-forward folds: train then test, stepped forward."""
    step = step_days or test_days
    train_ms = train_days * MS_PER_DAY
    test_ms = test_days * MS_PER_DAY
    step_ms = step * MS_PER_DAY
    folds: list[dict[str, int]] = []
    cursor = start_ms
    while cursor + train_ms + test_ms <= end_ms:
        folds.append(
            {
                "train_start_ms": cursor,
                "train_end_ms": cursor + train_ms,
                "test_start_ms": cursor + train_ms,
                "test_end_ms": cursor + train_ms + test_ms,
            }
        )
        cursor += step_ms
    return folds
