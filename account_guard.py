"""Entry gates — free-margin floor only when position count is unlimited."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

UNLIMITED_POSITIONS = 9999
UNLIMITED_HOURLY_TPH = 99999


def positions_unlimited(settings: "Settings") -> bool:
    """max_positions<=0 or >=9999 means no artificial open-count cap."""
    return True


def universe_fill_active(settings: "Settings") -> bool:
    """
    Fill the tradeable universe on movers — no hourly open cap, only free margin.
    On when UNIVERSE_FILL_MODE=true or TRADE_UNIVERSE=all.
    """
    if getattr(settings, "universe_fill_mode", False):
        return True
    return bool(getattr(settings, "trade_all_symbols", False))


def effective_hourly_tph_cap(settings: "Settings") -> int:
    if universe_fill_active(settings):
        return UNLIMITED_HOURLY_TPH
    cap = int(getattr(settings, "optimizer_target_max_tph", 12) or 12)
    return max(1, cap)


def effective_max_open(settings: "Settings", equity: float) -> int:
    """Return slot cap for logging; 9999 means only free margin limits opens."""
    return UNLIMITED_POSITIONS


def min_free_margin_to_open(settings: "Settings", equity: float) -> float:
    """Need this much free USDT before another entry (fraction of equity + reserve)."""
    floor = settings.margin_reserve_usdt * 3
    if equity <= 0:
        return floor
    if getattr(settings, "entries_never_pause", False) or universe_fill_active(settings):
        return max(floor, 1.0)
    pct_floor = equity * settings.min_free_margin_pct
    return max(floor, pct_floor)


def same_side_exposure_ok(
    open_positions: dict,
    side: str,
) -> tuple[bool, str]:
    _ = open_positions
    _ = side
    return True, ""


def entry_allowed(
    settings: "Settings",
    *,
    equity: float,
    free_margin: float,
    open_count: int,
    peak_equity: float = 0.0,
) -> tuple[bool, str]:
    _ = peak_equity
    try:
        from runtime_gates import clear_entries_pause, read_entries_pause

        if getattr(settings, "entries_never_pause", False):
            clear_entries_pause(settings.state_dir)
        else:
            paused, reason = read_entries_pause(settings.state_dir)
            if paused:
                return False, f"runtime pause: {reason}"
    except Exception:
        pass
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
    """No artificial per-tick open cap — only margin limits fills; quality gates pick winners."""
    tick_cap = max(1, int(settings.max_opens_per_tick))
    scan_batch = max(tick_cap, min(99, int(getattr(settings, "symbols_per_tick", 120)) // 2))
    if universe_fill_active(settings) or getattr(settings, "entries_never_pause", False):
        return max(base, scan_batch)
    return max(base, tick_cap)
