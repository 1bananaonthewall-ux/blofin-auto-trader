"""
Measure whether price is a steady directional runner vs choppy up-and-down.

Used to stay in coins that keep moving one way and avoid range whipsaw.
"""

from __future__ import annotations

from dataclasses import dataclass

from indicators import adx


@dataclass(frozen=True)
class RunQuality:
    path_efficiency_1m: float
    path_efficiency_5m: float
    chop_index: float
    trend_persistence: float
    runner_score: float
    is_runner: bool
    is_choppy: bool
    label: str


def _path_efficiency(closes: list[float], window: int) -> float:
    n = len(closes)
    if n < 10:
        return 0.5
    w = min(window, n - 1)
    seg = closes[-w - 1 :]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    if path <= 0:
        return 0.0
    return max(0.0, min(1.0, net / path))


def _chop_index(closes: list[float], window: int) -> float:
    n = len(closes)
    if n < 12:
        return 0.5
    w = min(window, n - 1)
    seg = closes[-w - 1 :]
    signs: list[int] = []
    for i in range(1, len(seg)):
        d = seg[i] - seg[i - 1]
        if d > 0:
            signs.append(1)
        elif d < 0:
            signs.append(-1)
    if len(signs) < 6:
        return 0.5
    flips = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    return max(0.0, min(1.0, flips / max(1, len(signs) - 1)))


def _trend_persistence(closes: list[float], window: int) -> float:
    n = len(closes)
    if n < 10:
        return 0.5
    w = min(window, n - 1)
    seg = closes[-w - 1 :]
    net = seg[-1] - seg[0]
    if abs(net) < 1e-12:
        return 0.35
    direction = 1 if net > 0 else -1
    aligned = 0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i - 1]
        if direction > 0 and d > 0:
            aligned += 1
        elif direction < 0 and d < 0:
            aligned += 1
    return aligned / max(1, len(seg) - 1)


def measure_run_quality(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]] | None = None,
    *,
    min_runner_score: float = 0.48,
    max_chop: float = 0.56,
    min_path_eff: float = 0.26,
) -> RunQuality | None:
    if len(ohlcv_1m) < 35:
        return None
    closes = [float(row[4]) for row in ohlcv_1m]
    if closes[-1] <= 0:
        return None

    pe1 = _path_efficiency(closes, 50)
    chop = _chop_index(closes, 40)
    persist = _trend_persistence(closes, 45)

    pe5 = pe1
    if ohlcv_5m and len(ohlcv_5m) >= 18:
        c5 = [float(row[4]) for row in ohlcv_5m]
        if c5[-1] > 0:
            pe5 = _path_efficiency(c5, min(24, len(c5) - 1))

    adx_val = adx(ohlcv_1m, 14) or 0.0
    adx_norm = max(0.0, min(1.0, adx_val / 38.0))

    runner_score = (
        0.38 * pe1
        + 0.22 * pe5
        + 0.18 * adx_norm
        + 0.12 * persist
        + 0.10 * max(0.0, 1.0 - chop)
    )
    runner_score = max(0.0, min(1.0, runner_score))

    is_choppy = (chop >= max_chop and pe1 < min_path_eff + 0.06) or pe1 < min_path_eff - 0.08
    is_runner = (
        runner_score >= min_runner_score
        and pe1 >= min_path_eff
        and chop <= max_chop + 0.04
        and persist >= 0.42
    )

    if is_runner:
        label = "runner"
    elif is_choppy:
        label = "choppy"
    else:
        label = "mixed"

    return RunQuality(
        path_efficiency_1m=round(pe1, 4),
        path_efficiency_5m=round(pe5, 4),
        chop_index=round(chop, 4),
        trend_persistence=round(persist, 4),
        runner_score=round(runner_score, 4),
        is_runner=is_runner,
        is_choppy=is_choppy,
        label=label,
    )
