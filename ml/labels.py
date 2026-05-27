"""Triple-barrier labels aligned with 3R scalp — train on trades that would win."""

from __future__ import annotations


def _long_hits_tp_first(
    ohlcv: list[list[float]],
    end: int,
    *,
    max_bars: int,
    stop_pct: float,
    take_pct: float,
) -> bool | None:
    entry = ohlcv[end][4]
    if entry <= 0:
        return None
    tp = entry * (1 + take_pct)
    sl = entry * (1 - stop_pct)
    for j in range(end + 1, min(end + 1 + max_bars, len(ohlcv))):
        h, l = ohlcv[j][2], ohlcv[j][3]
        if l <= sl:
            return False
        if h >= tp:
            return True
    return None


def _short_hits_tp_first(
    ohlcv: list[list[float]],
    end: int,
    *,
    max_bars: int,
    stop_pct: float,
    take_pct: float,
) -> bool | None:
    entry = ohlcv[end][4]
    if entry <= 0:
        return None
    tp = entry * (1 - take_pct)
    sl = entry * (1 + stop_pct)
    for j in range(end + 1, min(end + 1 + max_bars, len(ohlcv))):
        h, l = ohlcv[j][2], ohlcv[j][3]
        if h >= sl:
            return False
        if l <= tp:
            return True
    return None


def triple_barrier_direction(
    ohlcv_1m: list[list[float]],
    end: int,
    *,
    max_bars: int,
    stop_pct: float,
    take_pct: float,
) -> int | None:
    """
    Return 0=long winner, 1=short winner, None=skip (timeout or both fail).
    Mirrors live bot: 1R stop, 3R take path.
    """
    lw = _long_hits_tp_first(ohlcv_1m, end, max_bars=max_bars, stop_pct=stop_pct, take_pct=take_pct)
    sw = _short_hits_tp_first(ohlcv_1m, end, max_bars=max_bars, stop_pct=stop_pct, take_pct=take_pct)
    if lw is True and sw is not True:
        return 0
    if sw is True and lw is not True:
        return 1
    return None
