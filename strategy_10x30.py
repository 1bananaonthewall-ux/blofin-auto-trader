"""Dynamic Leverage Strategy — confidence-driven position sizing.

Concept:
  - Leverage scales dynamically with ML confidence (5x–50x via SmartPositionSizer)
  - Target 3% asset price move for strong reward:risk
  - Tight stop loss (1-1.5% asset move) to maintain 2:1+ reward-to-risk
  - Only enter when momentum/volume strongly supports a quick 3% move
  - Compound: reinvest all profits into next trade automatically
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from indicators import adx, ema, rsi, volume_ratio

log = logging.getLogger(__name__)


class Signal(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=False)
class StrategyDecision10x30:
    signal: Signal
    score: float        # 0-100+ quality score
    close: float         # current price
    entry_price: float   # price we'll enter at
    stop_pct: float      # % below/above entry for stop (asset move)
    take_pct: float      # % above/below entry for take profit (asset move)
    volume_ratio: float  # volume spike ratio
    adx_value: float     # trend strength (0-100)
    ema_spread_pct: float # EMA fast/slow spread as % of price
    funding_rate: float | None
    momentum_score: float # raw momentum strength

    @property
    def reward_risk_ratio(self) -> float:
        """Return reward:risk ratio based on asset moves (before leverage)."""
        if self.stop_pct <= 0:
            return 3.0
        return self.take_pct / self.stop_pct

    @property
    def leveraged_return_pct(self) -> float:
        """Return expected return % on margin (uses score/100 as dynamic leverage multiplier)."""
        lev = max(5, min(50, int(self.score / 100 * 50)))
        return self.take_pct * 100 * lev

    @property
    def leveraged_loss_pct(self) -> float:
        """Return expected loss % on margin (uses score/100 as dynamic leverage multiplier)."""
        lev = max(5, min(50, int(self.score / 100 * 50)))
        return self.stop_pct * 100 * lev


# ==== CONFIGURABLE PARAMETERS ====

# Target: 3% asset price move (leverage scales dynamically via SmartPositionSizer)
TARGET_TAKE_PCT = 0.03    # 3% asset price move
TARGET_STOP_PCT = 0.012   # 1.2% stop loss (2.5:1 reward:risk)

# Minimum thresholds for entry
MIN_SCORE = 55.0
MIN_VOLUME_RATIO = 1.3
MIN_ADX = 22            # minimum trend strength
MIN_EMA_SPREAD_PCT = 0.15  # minimum 0.15% spread between fast/slow EMA
MAX_SPREAD_PCT = 2.5      # cap spread scoring to avoid noise

# ATR-based dynamic adjustment
ATR_MIN_FOR_ENTRY = 0.005  # minimum 0.5% ATR to support quick 3% moves
ATR_TAKE_MULTIPLIER = 0.8   # if ATR is low, reduce take_pct proportionally

# Funding rate filters
MAX_FUNDING_LONG = 0.0005    # max funding for longs
MIN_FUNDING_SHORT = -0.0005  # min funding for shorts


def _ema_spread_score(fast_ema_val: float, slow_ema_val: float, close: float) -> tuple[float, float]:
    """Score based on EMA spread. Wider spread = stronger momentum."""
    spread_pct = abs(fast_ema_val - slow_ema_val) / close * 100
    # Score: 0 for 0% spread, scales up to 40 for 1%+ spread
    score = min(40.0, spread_pct * 40.0)
    return score, spread_pct


def _volume_score(vol_r: float) -> float:
    """Score based on volume ratio."""
    if vol_r < MIN_VOLUME_RATIO:
        return 0.0
    return min(25.0, (vol_r - 1.0) * 20.0)


def _adx_score(adx_val: float | None) -> float:
    """Score based on trend strength (ADX)."""
    if adx_val is None or adx_val < MIN_ADX:
        return 0.0
    # ADX 22 = 10pts, ADX 40 = 25pts cap
    return min(25.0, max(0.0, (adx_val - MIN_ADX) * 1.5))


def _rsi_score(rsi_val: float, signal: Signal) -> float:
    """Score based on RSI positioning."""
    if signal == Signal.LONG:
        # Prefer RSI 35-60 for upside room without being overbought
        if 35 <= rsi_val <= 60:
            return 15.0
        elif 60 < rsi_val <= 70:
            return 8.0
        elif rsi_val > 70:
            return -10.0  # overbought
        elif 25 <= rsi_val < 35:
            return 10.0   # oversold bounce potential
        else:
            return 0.0
    else:  # SHORT
        if 40 <= rsi_val <= 65:
            return 15.0
        elif 30 <= rsi_val < 40:
            return 8.0
        elif rsi_val < 25:
            return -10.0  # oversold
        elif 65 < rsi_val <= 75:
            return 10.0
        else:
            return 0.0


def _funding_penalty(funding_rate: float | None, signal: Signal) -> float:
    """Penalty for unfavorable funding rates."""
    if funding_rate is None:
        return 0.0
    if signal == Signal.LONG and funding_rate > MAX_FUNDING_LONG:
        return -20.0
    if signal == Signal.SHORT and funding_rate < MIN_FUNDING_SHORT:
        return -20.0
    return 0.0


def evaluate_10x30(ohlcv_1m: list[list[float]], ohlcv_5m: list[list[float]] | None = None,
                   *, funding_rate: float | None = None) -> StrategyDecision10x30 | None:
    """
    Evaluate a symbol for a dynamic leverage momentum trade.
    Leverage scales with score confidence via SmartPositionSizer.

    Returns a decision with signal + scores, or None if insufficient data.
    """
    if len(ohlcv_1m) < 35:
        return None

    closes = [row[4] for row in ohlcv_1m]
    volumes = [row[5] if len(row) > 5 else 0.0 for row in ohlcv_1m]
    closes_5m = [row[4] for row in ohlcv_5m] if ohlcv_5m else []

    # Calculate indicators on 1m data
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rs = rsi(closes, 14)
    i = len(closes) - 1

    if fast[i] is None or slow[i] is None or rs[i] is None:
        return None

    close = closes[i]
    fast_v, slow_v, rsi_v = fast[i], slow[i], rs[i]
    vol_r = volume_ratio(volumes)

    # Determine base signal direction from EMA crossover + RSI
    signal = Signal.FLAT
    momentum_raw = 0.0

    # Check EMA alignment
    ema_bullish = fast_v > slow_v
    ema_bearish = fast_v < slow_v

    # Higher timeframe alignment (5m)
    htf_bullish = False
    htf_bearish = False
    if len(closes_5m) >= 25:
        fast_5m = ema(closes_5m, 9)
        slow_5m = ema(closes_5m, 21)
        j = len(closes_5m) - 1
        if fast_5m[j] is not None and slow_5m[j] is not None:
            htf_bullish = fast_5m[j] > slow_5m[j]
            htf_bearish = fast_5m[j] < slow_5m[j]

    # ADX for trend strength
    adx_val = adx(ohlcv_1m, 14)

    # === LONG SIGNAL ===
    if ema_bullish and rsi_v < 72 and rsi_v > 30:
        signal = Signal.LONG
        # Momentum: how strongly price is pushing above EMAs
        momentum_raw = (close - fast_v) / fast_v * 100  # % above fast EMA

    # === SHORT SIGNAL ===
    elif ema_bearish and rsi_v > 28 and rsi_v < 70:
        signal = Signal.SHORT
        momentum_raw = (slow_v - close) / close * 100  # % below slow EMA

    if signal == Signal.FLAT:
        return StrategyDecision10x30(
            signal=Signal.FLAT, score=0.0, close=close,
            entry_price=close, stop_pct=TARGET_STOP_PCT,
            take_pct=TARGET_TAKE_PCT, volume_ratio=vol_r,
            adx_value=adx_val or 0, ema_spread_pct=0,
            funding_rate=funding_rate, momentum_score=0,
        )

    # === SCORING ===
    ema_score, ema_spread = _ema_spread_score(fast_v, slow_v, close)
    vol_score = _volume_score(vol_r)
    adx_score_val = _adx_score(adx_val)
    rsi_score_val = _rsi_score(rsi_v, signal)
    fund_penalty = _funding_penalty(funding_rate, signal)

    # Higher timeframe bonus
    htf_bonus = 0.0
    if signal == Signal.LONG and htf_bullish:
        htf_bonus = 15.0
    elif signal == Signal.SHORT and htf_bearish:
        htf_bonus = 15.0

    # Momentum bonus (price pushing hard in direction)
    momentum_bonus = 0.0
    if signal == Signal.LONG and momentum_raw > 0.1:
        momentum_bonus = min(15.0, momentum_raw * 5.0)
    elif signal == Signal.SHORT and momentum_raw > 0.1:
        momentum_bonus = min(15.0, momentum_raw * 5.0)

    total_score = ema_score + vol_score + adx_score_val + rsi_score_val + htf_bonus + momentum_bonus + fund_penalty

    # Penalty for conflicting HTF
    if (signal == Signal.LONG and htf_bearish) or (signal == Signal.SHORT and htf_bullish):
        total_score *= 0.35

    # Volume filter - must have enough volume for quick entry/exit
    if vol_r < MIN_VOLUME_RATIO:
        total_score *= 0.3

    # Final signal based on score threshold
    if total_score < MIN_SCORE:
        signal = Signal.FLAT

    return StrategyDecision10x30(
        signal=signal,
        score=round(total_score, 1),
        close=close,
        entry_price=close,
        stop_pct=TARGET_STOP_PCT,
        take_pct=TARGET_TAKE_PCT,
        volume_ratio=vol_r,
        adx_value=adx_val or 0,
        ema_spread_pct=round(ema_spread, 3),
        funding_rate=funding_rate,
        momentum_score=round(momentum_raw, 2),
    )