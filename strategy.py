from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from indicators import atr, ema, rsi, volume_ratio, macd, bollinger_bands, adx, mfi, chaikin_money_flow


class Signal(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=False)
class StrategyDecision:
    signal: Signal
    score: float
    fast_ema: float
    slow_ema: float
    rsi: float
    close: float
    stop_pct: float
    take_pct: float
    volume_ratio: float
    htf_aligned: bool
    funding_rate: float | None
    model_confidence: float = 0.0  # Model's confidence (0-1)
    leveraged_rr: float = 0.0     # Leverage-aware reward:risk ratio
    regime: str = "normal"        # Market regime: trending, ranging, volatile
    vwap_distance_pct: float = 0.0  # Distance from VWAP as %
    confluence_score: float = 0.0
    confluence_zone: str = ""
    confluence_agreeing: int = 0
    confluence_opposing: int = 0
    winner_tier: str = ""
    winner_score: float = 0.0
    pick_score: float = 0.0
    fast_win_score: float = 0.0


def _htf_bias(closes_5m: list[float]) -> str | None:
    if len(closes_5m) < 25:
        return None
    fast = ema(closes_5m, 9)
    slow = ema(closes_5m, 21)
    i = len(closes_5m) - 1
    if fast[i] is None or slow[i] is None:
        return None
    if fast[i] > slow[i]:
        return "long"
    if fast[i] < slow[i]:
        return "short"
    return None


def _detect_regime(ohlcv_1m: list[list[float]]) -> str:
    """Detect market regime: trending, ranging, or volatile.
    
    Uses ADX for trend strength and ATR for volatility.
    """
    closes = [row[4] for row in ohlcv_1m]
    adx_val = adx(ohlcv_1m, 14) or 0
    atr_val = atr(ohlcv_1m, 14) or 0
    close = closes[-1]
    atr_pct = atr_val / close if close > 0 else 0
    
    # Volatile: high ATR relative to price
    if atr_pct > 0.03:
        return "volatile"
    
    # Trending: strong ADX
    if adx_val > 25:
        return "trending"
    
    # Ranging: weak ADX
    return "ranging"


def _vwap(ohlcv: list[list[float]]) -> float:
    """Calculate Volume-Weighted Average Price."""
    total_vol = 0.0
    total_pv = 0.0
    for row in ohlcv:
        high = row[2]
        low = row[3]
        close = row[4]
        vol = row[5] if len(row) > 5 else 0.0
        typical_price = (high + low + close) / 3.0
        total_pv += typical_price * vol
        total_vol += vol
    if total_vol <= 0:
        return ohlcv[-1][4] if ohlcv else 0.0
    return total_pv / total_vol


def _fee_aware_adjust(stop_pct, take_pct, equity, small_account_threshold, min_take_profit_pct, fee_roundtrip_pct,
                      model_confidence=0.0, leverage=5, regime="normal", min_rr_override: float | None = None):
    """
    Enhanced SL/TP adjustment that tightens stops when confidence is high.
    Also adjusts based on market regime.
    """
    min_tp = max(min_take_profit_pct, fee_roundtrip_pct / 100.0)
    new_stop = stop_pct
    new_take = take_pct

    # Regime-based adjustment
    if regime == "volatile":
        # Wider stops in volatile markets to avoid noise
        stop_mult = 1.3
        take_mult = 1.5
    elif regime == "ranging":
        # Tighter stops in ranging markets
        stop_mult = 0.85
        take_mult = 0.9
    else:  # trending
        stop_mult = 1.0
        take_mult = 1.0

    # Confidence-based stop tightening (disabled in strict 3R — fixed R distance)
    if min_rr_override is None or min_rr_override < 2.5:
        if model_confidence > 0.8:
            stop_mult *= 0.65
        elif model_confidence > 0.7:
            stop_mult *= 0.80
        elif model_confidence > 0.6:
            stop_mult *= 0.90

    new_stop = stop_pct * stop_mult

    if min_rr_override is not None:
        min_rr = min_rr_override
    elif leverage >= 40:
        min_rr = 1.35
    elif leverage >= 25:
        min_rr = 1.45
    elif leverage >= 20:
        min_rr = 1.55
    elif leverage >= 15:
        min_rr = 1.65
    elif leverage >= 10:
        min_rr = 1.8
    elif leverage >= 7:
        min_rr = 2.0
    else:
        min_rr = 1.8

    if new_take < new_stop * min_rr:
        new_take = new_stop * min_rr

    if new_take < min_tp:
        new_take = min_tp
        if new_stop > new_take / min_rr:
            new_stop = new_take / min_rr

    # Clamp values
    if min_rr_override is not None and min_rr_override >= 2.5:
        max_stop = 0.018
        min_stop = 0.005
        max_take_cap = 0.08
    else:
        max_stop = 0.06 if model_confidence > 0.8 else 0.08
        min_stop = 0.006 if model_confidence > 0.8 else 0.01
        max_take_cap = 0.25 * take_mult
    new_stop = min(max_stop * stop_mult, max(min_stop, new_stop))
    new_take = min(max_take_cap, max(new_stop * min_rr, new_take))

    return new_stop, new_take, min_rr


def evaluate_enhanced(ohlcv_1m, ohlcv_5m, *, funding_rate=None,
                      min_score=50.0, min_volume_ratio=1.0,
                      atr_stop_mult=1.8, atr_take_mult=3.5,
                      max_funding_long=0.0005, min_funding_short=-0.0005,
                      equity=None, small_account_threshold=10.0,
                      fee_roundtrip_pct=0.08, min_take_profit_pct=0.02,
                      model_confidence=0.0, leverage=5):
    """
    Enhanced strategy with multi-timeframe confirmation, regime detection,
    VWAP awareness, ATR-based SL/TP, and leverage-aware scoring.
    """
    if len(ohlcv_1m) < 35:
        return None

    closes = [row[4] for row in ohlcv_1m]
    volumes = [row[5] if len(row) > 5 else 0.0 for row in ohlcv_1m]
    closes_5m = [row[4] for row in ohlcv_5m] if ohlcv_5m else []

    # ----- TECHNICAL INDICATORS -----
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rs = rsi(closes, 14)
    i = len(closes) - 1
    if fast[i] is None or slow[i] is None or rs[i] is None:
        return None

    close = closes[i]
    fast_v, slow_v, rsi_v = fast[i], slow[i], rs[i]
    vol_r = volume_ratio(volumes)
    htf = _htf_bias(closes_5m)

    # Regime detection
    regime = _detect_regime(ohlcv_1m)

    # VWAP
    vwap_price = _vwap(ohlcv_1m)
    vwap_dist = (close - vwap_price) / vwap_price if vwap_price > 0 else 0.0

    # MACD confirmation
    macd_line, macd_sig, macd_hist = macd(closes)
    macd_bullish = macd_hist[i] is not None and macd_hist[i] > 0
    macd_bearish = macd_hist[i] is not None and macd_hist[i] < 0

    # Bollinger Band position
    bb_mid, bb_upper, bb_lower, bb_pct_arr = bollinger_bands(closes)
    bb_pct = bb_pct_arr[i] if bb_pct_arr[i] is not None else 0.5

    # MFI confirmation
    mfi_val = mfi(ohlcv_1m)
    mfi_bullish = mfi_val is not None and mfi_val > 50
    mfi_bearish = mfi_val is not None and mfi_val < 50

    # CMF confirmation
    cmf_val = chaikin_money_flow(ohlcv_1m)
    cmf_bullish = cmf_val is not None and cmf_val > 0.05
    cmf_bearish = cmf_val is not None and cmf_val < -0.05

    # ATR-based SL/TP
    atr_v = atr(ohlcv_1m, 14)
    if atr_v is None or close <= 0:
        stop_pct, take_pct = 0.015, 0.03
    else:
        atr_pct = atr_v / close
        # Widen stops in volatile regime, tighten in ranging
        if regime == "volatile":
            stop_pct = min(0.08, max(0.015, atr_pct * atr_stop_mult * 1.3))
            take_pct = min(0.20, max(stop_pct * 2.0, atr_pct * atr_take_mult * 1.5))
        elif regime == "ranging":
            stop_pct = min(0.04, max(0.008, atr_pct * atr_stop_mult * 0.85))
            take_pct = min(0.10, max(stop_pct * 2.0, atr_pct * atr_take_mult * 0.9))
        else:
            stop_pct = min(0.05, max(0.01, atr_pct * atr_stop_mult))
            take_pct = min(0.15, max(stop_pct * 2.0, atr_pct * atr_take_mult))

    stop_pct, take_pct, min_rr = _fee_aware_adjust(
        stop_pct, take_pct, equity,
        small_account_threshold, min_take_profit_pct, fee_roundtrip_pct,
        model_confidence=model_confidence, leverage=leverage, regime=regime,
    )

    # Determine signal
    signal = Signal.FLAT
    
    # Multi-timeframe confirmation for LONG
    if fast_v > slow_v and rsi_v < 75:
        # Check MACD bullish
        if macd_bullish or mfi_bullish:
            signal = Signal.LONG
    
    # Multi-timeframe confirmation for SHORT
    if fast_v < slow_v and rsi_v > 25:
        if macd_bearish or mfi_bearish:
            signal = Signal.SHORT

    if signal == Signal.FLAT:
        return StrategyDecision(signal=signal, score=0.0,
            fast_ema=fast_v, slow_ema=slow_v, rsi=rsi_v, close=close,
            stop_pct=stop_pct, take_pct=take_pct, volume_ratio=vol_r,
            htf_aligned=False, funding_rate=funding_rate,
            model_confidence=model_confidence, leveraged_rr=0.0,
            regime=regime, vwap_distance_pct=vwap_dist)

    spread_pct = abs(fast_v - slow_v) / close * 100
    score = spread_pct * 15 + max(0, vol_r - 1) * 20

    # Signal-specific scoring
    if signal == Signal.LONG:
        score += max(0, 55 - rsi_v) * 0.5
        if htf == "long":
            score += 25
        if funding_rate is not None and funding_rate > max_funding_long:
            score -= 25
        # VWAP bonus: price above VWAP = bullish
        if vwap_dist > 0:
            score += 10
        # MACD bonus
        if macd_bullish:
            score += 15
        # MFI confirmation
        if mfi_bullish:
            score += 10
        # CMF confirmation
        if cmf_bullish:
            score += 10
        # Bollinger Band position - prefer price in lower half
        if bb_pct < 0.5:
            score += 10
    else:
        score += max(0, rsi_v - 45) * 0.5
        if htf == "short":
            score += 25
        if funding_rate is not None and funding_rate < min_funding_short:
            score -= 25
        # VWAP bonus: price below VWAP = bearish
        if vwap_dist < 0:
            score += 10
        # MACD confirmation
        if macd_bearish:
            score += 15
        # MFI confirmation
        if mfi_bearish:
            score += 10
        # CMF confirmation
        if cmf_bearish:
            score += 10
        # Bollinger Band position - prefer price in upper half
        if bb_pct > 0.5:
            score += 10

    htf_aligned = (signal == Signal.LONG and htf == "long") or (signal == Signal.SHORT and htf == "short")

    if vol_r < min_volume_ratio:
        score *= 0.5
    if not htf_aligned and htf is not None:
        score *= 0.35

    # Confidence scoring
    if model_confidence > 0.85:
        score *= 2.0
    elif model_confidence > 0.75:
        score *= 1.6
    elif model_confidence > 0.65:
        score *= 1.3
    elif model_confidence > 0.55:
        score *= 1.1
    elif model_confidence > 0.0:
        score *= 0.8

    # Reward:Risk ratio bonus
    rr = take_pct / max(stop_pct, 0.001)
    if rr > 3.0:
        score *= 1.3
    elif rr > 2.5:
        score *= 1.15

    # Leverage-aware scoring
    if leverage >= 15:
        min_score_effective = max(min_score, 65)
    elif leverage >= 10:
        min_score_effective = max(min_score, 55)
    else:
        min_score_effective = min_score

    if score < min_score_effective:
        signal = Signal.FLAT

    return StrategyDecision(signal=signal, score=round(score, 2),
        fast_ema=fast_v, slow_ema=slow_v, rsi=rsi_v, close=close,
        stop_pct=stop_pct, take_pct=take_pct, volume_ratio=vol_r,
        htf_aligned=htf_aligned, funding_rate=funding_rate,
        model_confidence=model_confidence,
        leveraged_rr=round(rr * leverage, 1),
        regime=regime, vwap_distance_pct=vwap_dist)


def evaluate_momentum(closes):
    ohlcv = [[0, 0, 0, 0, c, 0] for c in closes]
    return evaluate_enhanced(ohlcv, [])