"""Entry gates — free-margin floor only when position count is unlimited."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

UNLIMITED_POSITIONS = 9999


def positions_unlimited(settings: "Settings") -> bool:
    """max_positions<=0 or >=9999 means no artificial open-count cap."""
    return settings.max_positions <= 0 or settings.max_positions >= UNLIMITED_POSITIONS


def effective_max_open(settings: "Settings", equity: float) -> int:
    """Return slot cap for logging; 9999 means only free margin limits opens."""
    if positions_unlimited(settings):
        return UNLIMITED_POSITIONS
    cap = max(1, settings.max_positions)
    if equity > 0 and equity < settings.small_account_threshold:
        if settings.small_account_max_open <= 0 or settings.small_account_max_open >= UNLIMITED_POSITIONS:
            return UNLIMITED_POSITIONS
        cap = min(cap, max(1, settings.small_account_max_open))
    return cap


def min_free_margin_to_open(settings: "Settings", equity: float) -> float:
    """Need this much free USDT before another entry (fraction of equity + reserve)."""
    floor = settings.margin_reserve_usdt * 3
    if equity <= 0:
        return floor
    pct_floor = equity * settings.min_free_margin_pct
    if equity < settings.small_account_threshold:
        pct_floor = max(pct_floor, equity * settings.small_account_min_free_pct)
    return max(floor, pct_floor)


def same_side_exposure_ok(
    open_positions: dict,
    side: str,
    *,
    max_same_side: int,
) -> tuple[bool, str]:
    if max_same_side <= 0:
        return True, ""
    side = side.lower()
    count = sum(1 for p in open_positions.values() if str(p.get("side", "")).lower() == side)
    if count >= max_same_side:
        return False, f"max {max_same_side} {side} positions already open ({count})"
    return True, ""


def entry_allowed(
    settings: "Settings",
    *,
    equity: float,
    free_margin: float,
    open_count: int,
) -> tuple[bool, str]:
    if settings.entries_paused:
        return False, "ENTRIES_PAUSED=true (steward still manages open positions)"

    if not positions_unlimited(settings):
        cap = effective_max_open(settings, equity)
        if open_count >= cap:
            return False, f"at max open positions ({open_count}/{cap})"

    need_free = min_free_margin_to_open(settings, equity)
    if free_margin < need_free:
        return False, f"free ${free_margin:.2f} < need ${need_free:.2f} to add risk"

    return True, ""


def effective_max_opens_per_tick(settings: "Settings", equity: float, base: int) -> int:
    n = max(1, min(base, settings.max_opens_per_tick))
    if positions_unlimited(settings):
        return n
    if equity > 0 and equity < settings.small_account_threshold:
        if settings.small_account_max_opens_per_tick <= 0:
            return n
        n = min(n, settings.small_account_max_opens_per_tick)
    return max(1, n)
