"""Backtest date range resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MAX_LOOKBACK_DAYS = 730
MIN_LOOKBACK_DAYS = 7
MS_PER_DAY = 86_400_000


def _parse_date_utc(date_str: str) -> datetime:
    return datetime.strptime(date_str.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def resolve_backtest_range(
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
        if span > MAX_LOOKBACK_DAYS:
            raise ValueError(f"Date range cannot exceed {MAX_LOOKBACK_DAYS} days (2 years)")
        if span < MIN_LOOKBACK_DAYS:
            raise ValueError(f"Date range must be at least {MIN_LOOKBACK_DAYS} days")
        return {
            "start_ms": int(start_dt.timestamp() * 1000),
            "end_ms": int(end_dt.timestamp() * 1000),
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "lookback_days": span,
        }

    days = max(MIN_LOOKBACK_DAYS, min(MAX_LOOKBACK_DAYS, int(lookback_days or 365)))
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - days * MS_PER_DAY
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": now.strftime("%Y-%m-%d"),
        "lookback_days": days,
    }
