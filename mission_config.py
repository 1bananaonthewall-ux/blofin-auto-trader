"""
Single source of truth for the engine's only objective.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Sole target — every subsystem imports from here
TARGET_CAPITAL_USD = 95_000_000.0
TARGET_DATE_ISO = "2027-09-01"
START_CAPITAL_REFERENCE = 100.0

TARGET_DATE_TS = datetime.strptime(TARGET_DATE_ISO, "%Y-%m-%d").replace(
    tzinfo=timezone.utc
).timestamp()


def target_date_iso() -> str:
    return TARGET_DATE_ISO


def target_date_ts() -> float:
    return TARGET_DATE_TS


def sole_objective_label() -> str:
    return f"${TARGET_CAPITAL_USD:,.0f} by {TARGET_DATE_ISO}"
