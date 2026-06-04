"""Account margin mode helpers (isolated vs cross)."""

from __future__ import annotations


def normalize_margin_mode(raw: str | None) -> str:
    m = str(raw or "isolated").strip().lower()
    if m in ("cross", "crossed", "cross_margin", "crossmargin"):
        return "cross"
    return "isolated"


def is_cross_margin(raw: str | None) -> bool:
    return normalize_margin_mode(raw) == "cross"
