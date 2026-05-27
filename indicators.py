from __future__ import annotations

import math


def ema(values: list[float], period: int) -> list[float | None]:
    if period < 1 or not values or len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)
    out: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def atr(ohlcv: list[list[float]], period: int = 14) -> float | None:
    if len(ohlcv) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(ohlcv)):
        _, _o, high, low, close = ohlcv[i][:5]
        prev_close = ohlcv[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def volume_ratio(volumes: list[float], lookback: int = 20) -> float:
    if len(volumes) < lookback + 1:
        return 1.0
    recent = volumes[-1]
    avg = sum(volumes[-lookback - 1 : -1]) / lookback
    if avg <= 0:
        return 1.0
    return recent / avg


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ==== NEW INDICATORS ====

def macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return (MACD line, Signal line, Histogram)."""
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]
    sig_line = ema([v for v in macd_line if v is not None], signal) if any(v is not None for v in macd_line) else []
    # Re-align signal line to original length
    sig_aligned: list[float | None] = [None] * len(values)
    sig_idx = 0
    valid_macd = [v for v in macd_line if v is not None]
    valid_count = len(valid_macd)
    sig_valid = [v for v in sig_line if v is not None]
    # Find first valid MACD index
    first_valid = -1
    for i, v in enumerate(macd_line):
        if v is not None:
            first_valid = i
            break
    if first_valid >= 0 and sig_line:
        # Place signal line values at same positions as valid MACD
        sig_ptr = 0
        for i in range(first_valid, len(values)):
            if macd_line[i] is not None and sig_ptr < len(sig_line) and sig_line[sig_ptr] is not None:
                sig_aligned[i] = sig_line[sig_ptr]
                sig_ptr += 1

    hist: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if macd_line[i] is not None and sig_aligned[i] is not None:
            hist[i] = macd_line[i] - sig_aligned[i]
    return macd_line, sig_aligned, hist


def bollinger_bands(
    values: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    """Return (middle, upper, lower, %b)."""
    middle = ema(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    bb_pct: list[float | None] = [None] * len(values)

    for i in range(period - 1, len(values)):
        if middle[i] is None:
            continue
        window = values[i - period + 1 : i + 1]
        std = _std(window)
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
        denom = upper[i] - lower[i]
        if denom > 0:
            bb_pct[i] = (values[i] - lower[i]) / denom
        else:
            bb_pct[i] = 0.5
    return middle, upper, lower, bb_pct


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


def adx(ohlcv: list[list[float]], period: int = 14) -> float | None:
    """Return ADX (trend strength) value at the last bar."""
    if len(ohlcv) < period + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_values: list[float] = []
    for i in range(1, len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        prev_high = ohlcv[i - 1][2]
        prev_low = ohlcv[i - 1][3]
        prev_close = ohlcv[i - 1][4]
        up_move = high - prev_high
        down_move = prev_low - low
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)
        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)

    if len(tr_values) < period:
        return None

    # Smoothed ATR and DM
    atr_val = sum(tr_values[-period:]) / period
    plus_smooth = sum(plus_dm[-period:]) / period
    minus_smooth = sum(minus_dm[-period:]) / period

    if atr_val <= 0:
        return 0.0
    plus_di = 100 * plus_smooth / atr_val
    minus_di = 100 * minus_smooth / atr_val
    di_diff = abs(plus_di - minus_di)
    di_sum = plus_di + minus_di
    if di_sum <= 0:
        return 0.0
    dx = 100 * di_diff / di_sum
    return dx


def mfi(ohlcv: list[list[float]], period: int = 14) -> float | None:
    """Money Flow Index - volume-weighted RSI."""
    if len(ohlcv) < period + 1:
        return None
    typical_prices: list[float] = []
    raw_money_flow: list[float] = []
    for i in range(len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        close = ohlcv[i][4]
        tp = (high + low + close) / 3.0
        typical_prices.append(tp)
        vol = ohlcv[i][5] if len(ohlcv[i]) > 5 else 0.0
        raw_money_flow.append(tp * vol)

    positive_flow: list[float] = []
    negative_flow: list[float] = []
    for i in range(1, len(typical_prices)):
        if typical_prices[i] > typical_prices[i - 1]:
            positive_flow.append(raw_money_flow[i])
            negative_flow.append(0.0)
        elif typical_prices[i] < typical_prices[i - 1]:
            positive_flow.append(0.0)
            negative_flow.append(raw_money_flow[i])
        else:
            positive_flow.append(0.0)
            negative_flow.append(0.0)

    if len(positive_flow) < period:
        return None

    pos_sum = sum(positive_flow[-period:])
    neg_sum = sum(negative_flow[-period:])
    if neg_sum <= 0:
        return 100.0 if pos_sum > 0 else 50.0
    ratio = pos_sum / neg_sum
    return 100.0 - (100.0 / (1.0 + ratio))


def chaikin_money_flow(ohlcv: list[list[float]], period: int = 20) -> float | None:
    """Chaikin Money Flow. Positive = buying pressure."""
    if len(ohlcv) < period:
        return None
    cmf_values: list[float] = []
    for i in range(len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        close = ohlcv[i][4]
        vol = ohlcv[i][5] if len(ohlcv[i]) > 5 else 0.0
        if high == low:
            mfv = 0.0
        else:
            mfv = ((close - low) - (high - close)) / (high - low) * vol
        cmf_values.append(mfv)
    if len(cmf_values) < period:
        return None
    vol_list = [row[5] if len(row) > 5 else 0.0 for row in ohlcv]
    vol_sum = sum(vol_list[-period:])
    if vol_sum <= 0:
        return 0.0
    return sum(cmf_values[-period:]) / vol_sum


def williams_r(ohlcv: list[list[float]], period: int = 14) -> float | None:
    if len(ohlcv) < period:
        return None
    window = ohlcv[-period:]
    highs = [b[2] for b in window]
    lows = [b[3] for b in window]
    close = ohlcv[-1][4]
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return -50.0
    return -100.0 * (hi - close) / (hi - lo)


def roc(closes: list[float], period: int = 10) -> float | None:
    if len(closes) <= period or closes[-period - 1] == 0:
        return None
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1]


def stochastic_k(ohlcv: list[list[float]], period: int = 14) -> float | None:
    if len(ohlcv) < period:
        return None
    window = ohlcv[-period:]
    highs = [b[2] for b in window]
    lows = [b[3] for b in window]
    close = ohlcv[-1][4]
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return 50.0
    return 100.0 * (close - lo) / (hi - lo)