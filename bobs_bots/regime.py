"""HTF trend and period bias for backtest + live alignment."""

from __future__ import annotations

from indicators import adx, ema
from strategy import Signal


def htf_ema_bias(closes_1h: list[float]) -> str | None:
    if len(closes_1h) < 25:
        return None
    fast = ema(closes_1h, 9)
    slow = ema(closes_1h, 21)
    i = len(closes_1h) - 1
    if fast[i] is None or slow[i] is None:
        return None
    if fast[i] > slow[i] * 1.0005:
        return "long"
    if fast[i] < slow[i] * 0.9995:
        return "short"
    return None


def _bias_from_segment(seg: list[list[float]], threshold: float = 0.018) -> str:
    if len(seg) < 8:
        return "neutral"
    o, cl = seg[0][4], seg[-1][4]
    if o <= 0:
        return "neutral"
    ret = (cl - o) / o
    if ret > threshold:
        return "bull"
    if ret < -threshold:
        return "bear"
    return "neutral"


def period_bias(candles_1h: list[list[float]], start_ms: int, end_ms: int) -> str:
    """Dominant direction for a backtest window (summary / display)."""
    return rolling_period_bias(candles_1h, end_ms, start_ms=start_ms)


def rolling_period_bias(
    candles_1h: list[list[float]],
    as_of_ms: int,
    *,
    start_ms: int | None = None,
) -> str:
    """Direction as of a bar — recent windows first."""
    for days, threshold in ((7, 0.005), (21, 0.008)):
        ws = as_of_ms - days * 86_400_000
        seg = [c for c in candles_1h if ws <= c[0] <= as_of_ms]
        b = _bias_from_segment(seg, threshold)
        if b != "neutral":
            return b
    if start_ms is not None:
        full = [c for c in candles_1h if start_ms <= c[0] <= as_of_ms]
        b = _bias_from_segment(full, 0.014)
        if b != "neutral":
            return b
    return "neutral"



def htf_adx(ohlcv_1h: list[list[float]]) -> float:
    if len(ohlcv_1h) < 20:
        return 0.0
    return adx(ohlcv_1h, 14) or 0.0


def signal_allowed(
    signal: Signal,
    *,
    htf_bias: str | None,
    period: str,
    require_htf_align: bool,
    trend_with_period: bool,
    trend_only: bool,
    htf_aligned: bool,
    min_adx: float = 14.0,
    adx_val: float = 0.0,
) -> bool:
    """Quality gates only — direction comes from TA confluence, not macro period bias."""
    if signal == Signal.FLAT:
        return False
    if adx_val > 0 and adx_val < min_adx:
        return False

    side = signal.value
    # Optional macro filters (off by default — TA long/short is authoritative).
    if trend_only and period == "neutral":
        return False
    if trend_only and period in ("bull", "bear"):
        if period == "bull" and side != "long":
            return False
        if period == "bear" and side != "short":
            return False
    if trend_with_period and period in ("bull", "bear"):
        if period == "bull" and side != "long":
            return False
        if period == "bear" and side != "short":
            return False

    if require_htf_align:
        if htf_bias and htf_bias != side:
            return False
        if not htf_aligned and htf_bias is None:
            return False

    return True
