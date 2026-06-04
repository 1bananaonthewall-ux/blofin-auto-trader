"""
Technical analysis confluence engine.

Runs many independent TA methods every scan, measures agreement,
and ranks the combined directional conviction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from indicators import (
    adx,
    atr,
    bollinger_bands,
    chaikin_money_flow,
    ema,
    macd,
    mfi,
    rsi,
    volume_ratio,
)
from strategy import Signal, StrategyDecision, _detect_regime, _fee_aware_adjust, _htf_bias, _vwap

log = logging.getLogger(__name__)

MIN_CONFLUENCE_SCORE = 0.52
MIN_AGREEING_VOTES = 5


@dataclass
class TAVote:
    name: str
    signal: Signal
    strength: float
    weight: float
    detail: str = ""


@dataclass
class ConfluenceResult:
    direction: Signal
    confluence_score: float
    composite_score: float
    agreeing: list[str]
    opposing: list[str]
    neutral: list[str]
    votes_long: int
    votes_short: int
    total_weight_long: float
    total_weight_short: float
    close: float
    stop_pct: float
    take_pct: float
    regime: str
    htf_aligned: bool
    volume_ratio: float
    vwap_distance_pct: float
    fast_ema: float
    slow_ema: float
    rsi: float
    model_confidence: float
    leveraged_rr: float
    run_label: str = "mixed"
    run_score: float = 0.5
    path_efficiency: float = 0.5
    chop_index: float = 0.5
    is_runner: bool = False
    is_choppy: bool = False
    votes: list[TAVote] = field(default_factory=list)


def _vote(name: str, signal: Signal, strength: float, weight: float, detail: str = "") -> TAVote:
    return TAVote(name=name, signal=signal, strength=max(0.0, min(1.0, strength)), weight=weight, detail=detail)


def _ema_vote(closes: list[float], fast_p: int, slow_p: int, weight: float, name: str) -> TAVote:
    fast = ema(closes, fast_p)
    slow = ema(closes, slow_p)
    i = len(closes) - 1
    if fast[i] is None or slow[i] is None or closes[i] <= 0:
        return _vote(name, Signal.FLAT, 0.0, weight, "no data")
    spread = abs(fast[i] - slow[i]) / closes[i]
    if fast[i] > slow[i]:
        return _vote(name, Signal.LONG, min(1.0, 0.5 + spread * 80), weight, f"ema{fast_p}>{slow_p}")
    if fast[i] < slow[i]:
        return _vote(name, Signal.SHORT, min(1.0, 0.5 + spread * 80), weight, f"ema{fast_p}<{slow_p}")
    return _vote(name, Signal.FLAT, 0.0, weight, "flat")


def _rsi_vote(closes: list[float]) -> TAVote:
    rs = rsi(closes, 14)
    i = len(closes) - 1
    if rs[i] is None:
        return _vote("rsi", Signal.FLAT, 0.0, 1.0)
    v = rs[i]
    if v < 35:
        return _vote("rsi", Signal.LONG, min(1.0, (40 - v) / 25), 1.0, f"rsi={v:.0f} oversold")
    if v > 65:
        return _vote("rsi", Signal.SHORT, min(1.0, (v - 60) / 25), 1.0, f"rsi={v:.0f} overbought")
    if v < 48:
        return _vote("rsi", Signal.LONG, 0.35, 1.0, f"rsi={v:.0f} lean long")
    if v > 52:
        return _vote("rsi", Signal.SHORT, 0.35, 1.0, f"rsi={v:.0f} lean short")
    return _vote("rsi", Signal.FLAT, 0.0, 1.0, f"rsi={v:.0f} neutral")


def _macd_vote(closes: list[float]) -> TAVote:
    _, _, hist = macd(closes)
    i = len(closes) - 1
    if hist[i] is None or closes[i] <= 0:
        return _vote("macd", Signal.FLAT, 0.0, 1.1)
    h = hist[i] / closes[i] * 100
    if h > 0.02:
        return _vote("macd", Signal.LONG, min(1.0, abs(h) * 15), 1.1, f"hist+")
    if h < -0.02:
        return _vote("macd", Signal.SHORT, min(1.0, abs(h) * 15), 1.1, f"hist-")
    return _vote("macd", Signal.FLAT, 0.0, 1.1)


def _bb_vote(closes: list[float]) -> TAVote:
    _, _, _, pct = bollinger_bands(closes)
    i = len(closes) - 1
    if pct[i] is None:
        return _vote("bollinger", Signal.FLAT, 0.0, 0.9)
    p = pct[i]
    if p < 0.25:
        return _vote("bollinger", Signal.LONG, min(1.0, (0.35 - p) * 2), 0.9, f"%b={p:.2f} low")
    if p > 0.75:
        return _vote("bollinger", Signal.SHORT, min(1.0, (p - 0.65) * 2), 0.9, f"%b={p:.2f} high")
    return _vote("bollinger", Signal.FLAT, 0.2, 0.9, f"%b={p:.2f} mid")


def _vwap_vote(ohlcv_1m: list[list[float]]) -> TAVote:
    close = ohlcv_1m[-1][4]
    vw = _vwap(ohlcv_1m)
    if vw <= 0:
        return _vote("vwap", Signal.FLAT, 0.0, 1.1)
    dist = (close - vw) / vw
    if dist > 0.002:
        return _vote("vwap", Signal.LONG, min(1.0, abs(dist) * 200), 1.1, "above vwap")
    if dist < -0.002:
        return _vote("vwap", Signal.SHORT, min(1.0, abs(dist) * 200), 1.1, "below vwap")
    return _vote("vwap", Signal.FLAT, 0.0, 1.1)


def _htf_vote(closes_5m: list[float]) -> TAVote:
    bias = _htf_bias(closes_5m)
    if bias == "long":
        return _vote("htf_5m", Signal.LONG, 0.85, 1.4, "5m bull")
    if bias == "short":
        return _vote("htf_5m", Signal.SHORT, 0.85, 1.4, "5m bear")
    return _vote("htf_5m", Signal.FLAT, 0.0, 1.4)


def _adx_vote(ohlcv_1m: list[list[float]], closes: list[float]) -> TAVote:
    adx_v = adx(ohlcv_1m, 14) or 0
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    i = len(closes) - 1
    if fast[i] is None or slow[i] is None:
        return _vote("adx_trend", Signal.FLAT, 0.0, 1.2)
    trend_strength = min(1.0, adx_v / 40.0)
    if adx_v < 18:
        return _vote("adx_trend", Signal.FLAT, 0.0, 1.2, f"adx={adx_v:.0f} weak")
    if fast[i] > slow[i]:
        return _vote("adx_trend", Signal.LONG, trend_strength, 1.2, f"adx={adx_v:.0f} up")
    return _vote("adx_trend", Signal.SHORT, trend_strength, 1.2, f"adx={adx_v:.0f} down")


def _flow_vote(ohlcv_1m: list[list[float]]) -> list[TAVote]:
    mfi_v = mfi(ohlcv_1m)
    cmf_v = chaikin_money_flow(ohlcv_1m)
    votes = []
    if mfi_v is not None:
        if mfi_v > 55:
            votes.append(_vote("mfi", Signal.LONG, min(1.0, (mfi_v - 50) / 30), 1.0, f"mfi={mfi_v:.0f}"))
        elif mfi_v < 45:
            votes.append(_vote("mfi", Signal.SHORT, min(1.0, (50 - mfi_v) / 30), 1.0, f"mfi={mfi_v:.0f}"))
        else:
            votes.append(_vote("mfi", Signal.FLAT, 0.0, 1.0))
    if cmf_v is not None:
        if cmf_v > 0.06:
            votes.append(_vote("cmf", Signal.LONG, min(1.0, cmf_v * 5), 1.0, f"cmf+"))
        elif cmf_v < -0.06:
            votes.append(_vote("cmf", Signal.SHORT, min(1.0, abs(cmf_v) * 5), 1.0, f"cmf-"))
        else:
            votes.append(_vote("cmf", Signal.FLAT, 0.0, 1.0))
    return votes


def _structure_vote(ohlcv_1m: list[list[float]]) -> TAVote:
    if len(ohlcv_1m) < 12:
        return _vote("structure", Signal.FLAT, 0.0, 1.0)
    highs = [r[2] for r in ohlcv_1m[-8:]]
    lows = [r[3] for r in ohlcv_1m[-8:]]
    hh = highs[-1] > max(highs[:-1]) and lows[-1] > min(lows[:-1])
    ll = lows[-1] < min(lows[:-1]) and highs[-1] < max(highs[:-1])
    if hh:
        return _vote("structure", Signal.LONG, 0.75, 1.0, "higher highs")
    if ll:
        return _vote("structure", Signal.SHORT, 0.75, 1.0, "lower lows")
    return _vote("structure", Signal.FLAT, 0.0, 1.0)


def _volume_vote(volumes: list[float]) -> TAVote:
    vr = volume_ratio(volumes)
    if vr >= 1.3:
        return _vote("volume", Signal.FLAT, min(1.0, (vr - 1) / 2), 0.8, f"vol={vr:.1f}x")
    return _vote("volume", Signal.FLAT, 0.0, 0.8, f"vol={vr:.1f}x")


def _funding_vote(funding: float | None) -> TAVote:
    if funding is None:
        return _vote("funding", Signal.FLAT, 0.0, 0.9)
    if funding > 0.0003:
        return _vote("funding", Signal.SHORT, min(1.0, funding * 2000), 0.9, "crowded longs")
    if funding < -0.0003:
        return _vote("funding", Signal.LONG, min(1.0, abs(funding) * 2000), 0.9, "crowded shorts")
    return _vote("funding", Signal.FLAT, 0.0, 0.9)


def _ml_vote(ml_decision: StrategyDecision | None) -> TAVote:
    if ml_decision is None or ml_decision.signal == Signal.FLAT:
        return _vote("ml", Signal.FLAT, 0.0, 1.5)
    conf = ml_decision.model_confidence or (ml_decision.score / 100.0)
    return _vote("ml", ml_decision.signal, conf, 1.5, f"ml={conf:.2f}")


def run_all_analyses(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]],
    *,
    funding_rate: float | None = None,
    ml_decision: StrategyDecision | None = None,
) -> ConfluenceResult | None:
    """Evaluate every TA layer and compute weighted confluence."""
    if len(ohlcv_1m) < 35:
        return None

    closes = [row[4] for row in ohlcv_1m]
    volumes = [row[5] if len(row) > 5 else 0.0 for row in ohlcv_1m]
    closes_5m = [row[4] for row in ohlcv_5m] if ohlcv_5m else []
    close = closes[-1]
    regime = _detect_regime(ohlcv_1m)

    run_label = "mixed"
    run_score = 0.5
    path_efficiency = 0.5
    chop_index = 0.5
    is_runner = False
    is_choppy = False
    try:
        from run_quality import measure_run_quality

        rq = measure_run_quality(ohlcv_1m, ohlcv_5m)
        if rq:
            run_label = rq.label
            run_score = rq.runner_score
            path_efficiency = rq.path_efficiency_1m
            chop_index = rq.chop_index
            is_runner = rq.is_runner
            is_choppy = rq.is_choppy
            if is_runner and regime == "ranging":
                regime = "trending"
    except Exception:
        pass

    votes: list[TAVote] = [
        _ema_vote(closes, 9, 21, 1.2, "ema_1m"),
        _ema_vote(closes_5m, 9, 21, 1.3, "ema_5m") if len(closes_5m) >= 25 else _vote("ema_5m", Signal.FLAT, 0, 1.3),
        _rsi_vote(closes),
        _macd_vote(closes),
        _bb_vote(closes),
        _vwap_vote(ohlcv_1m),
        _htf_vote(closes_5m),
        _adx_vote(ohlcv_1m, closes),
        _structure_vote(ohlcv_1m),
        _volume_vote(volumes),
        _funding_vote(funding_rate),
        _ml_vote(ml_decision),
    ]
    votes.extend(_flow_vote(ohlcv_1m))

    long_w = short_w = 0.0
    for v in votes:
        if v.signal == Signal.LONG:
            long_w += v.strength * v.weight
        elif v.signal == Signal.SHORT:
            short_w += v.strength * v.weight

    if long_w < 0.01 and short_w < 0.01:
        return None

    if long_w >= short_w:
        direction = Signal.LONG
        win_w, lose_w = long_w, short_w
    else:
        direction = Signal.SHORT
        win_w, lose_w = short_w, long_w

    total_active = long_w + short_w
    confluence_score = win_w / total_active if total_active > 0 else 0.0

    agreeing = [v.name for v in votes if v.signal == direction and v.strength >= 0.25]
    opposing = [v.name for v in votes if v.signal != Signal.FLAT and v.signal != direction and v.strength >= 0.25]
    neutral = [v.name for v in votes if v.signal == Signal.FLAT or v.strength < 0.25]

    votes_long = sum(1 for v in votes if v.signal == Signal.LONG and v.strength >= 0.25)
    votes_short = sum(1 for v in votes if v.signal == Signal.SHORT and v.strength >= 0.25)

    if confluence_score < MIN_CONFLUENCE_SCORE:
        return None
    if len(agreeing) < MIN_AGREEING_VOTES:
        return None
    if len(opposing) >= len(agreeing):
        return None

    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rs = rsi(closes, 14)
    i = len(closes) - 1
    fast_v = fast[i] or close
    slow_v = slow[i] or close
    rsi_v = rs[i] or 50.0
    vol_r = volume_ratio(volumes)
    vwap_price = _vwap(ohlcv_1m)
    vwap_dist = (close - vwap_price) / vwap_price if vwap_price > 0 else 0.0
    htf = _htf_bias(closes_5m)
    htf_aligned = (direction == Signal.LONG and htf == "long") or (direction == Signal.SHORT and htf == "short")

    atr_v = atr(ohlcv_1m, 14)
    scalp = getattr(run_all_analyses, "_scalp_ctx", None)
    atr_stop_m = scalp["atr_stop_mult"] if scalp else 1.8
    atr_take_m = scalp["atr_take_mult"] if scalp else 3.5
    max_stop = scalp["max_stop_pct"] if scalp else 0.06
    max_take = scalp["max_take_pct"] if scalp else 0.18
    min_rr = float(scalp.get("min_rr", 1.35)) if scalp else 1.35
    three_r = bool(scalp.get("three_r_mode")) if scalp else False

    if atr_v is None or close <= 0:
        stop_pct, take_pct = (0.012, 0.036) if three_r else ((0.012, 0.024) if scalp else (0.015, 0.03))
    else:
        atr_pct = atr_v / close
        mult = 1.15 if regime == "volatile" and scalp else (1.3 if regime == "volatile" else (0.8 if regime == "ranging" and scalp else (0.85 if regime == "ranging" else 1.0)))
        stop_pct = min(max_stop, max(0.006 if scalp else 0.008, atr_pct * atr_stop_m * mult))
        if three_r:
            take_pct = min(max_take, stop_pct * min_rr)
        else:
            take_pct = min(max_take, max(stop_pct * min_rr, atr_pct * atr_take_m * mult))

    ml_conf = 0.0
    if ml_decision and ml_decision.signal == direction:
        ml_conf = ml_decision.model_confidence or (ml_decision.score / 100.0)

    stop_pct, take_pct, _ = _fee_aware_adjust(
        stop_pct, take_pct, None, 10.0, 0.02, 0.12,
        model_confidence=max(ml_conf, confluence_score), leverage=10, regime=regime,
        min_rr_override=min_rr if three_r else None,
    )

    composite = confluence_score * 55 + len(agreeing) * 6 + win_w * 12
    if ml_conf > 0:
        composite += ml_conf * 25
    if htf_aligned:
        composite += 12
    if vol_r >= 1.2:
        composite += 5
    composite = min(100.0, composite)

    rr = take_pct / max(stop_pct, 0.001)

    return ConfluenceResult(
        direction=direction,
        confluence_score=round(confluence_score, 4),
        composite_score=round(composite, 2),
        agreeing=agreeing,
        opposing=opposing,
        neutral=neutral,
        votes_long=votes_long,
        votes_short=votes_short,
        total_weight_long=round(long_w, 3),
        total_weight_short=round(short_w, 3),
        close=close,
        stop_pct=stop_pct,
        take_pct=take_pct,
        regime=regime,
        htf_aligned=htf_aligned,
        volume_ratio=vol_r,
        vwap_distance_pct=vwap_dist,
        fast_ema=fast_v,
        slow_ema=slow_v,
        rsi=rsi_v,
        model_confidence=max(ml_conf, confluence_score),
        leveraged_rr=round(rr * 10, 1),
        votes=votes,
    )


def confluence_to_decision(cf: ConfluenceResult) -> StrategyDecision:
    zone = "+".join(cf.agreeing[:6])
    if len(cf.agreeing) > 6:
        zone += f"+{len(cf.agreeing) - 6}more"
    dec = StrategyDecision(
        signal=cf.direction,
        score=cf.composite_score,
        fast_ema=cf.fast_ema,
        slow_ema=cf.slow_ema,
        rsi=cf.rsi,
        close=cf.close,
        stop_pct=cf.stop_pct,
        take_pct=cf.take_pct,
        volume_ratio=cf.volume_ratio,
        htf_aligned=cf.htf_aligned,
        funding_rate=None,
        model_confidence=cf.model_confidence,
        leveraged_rr=cf.leveraged_rr,
        regime=cf.regime,
        vwap_distance_pct=cf.vwap_distance_pct,
    )
    dec.confluence_score = cf.confluence_score
    dec.confluence_zone = zone
    dec.confluence_agreeing = len(cf.agreeing)
    dec.confluence_opposing = len(cf.opposing)
    dec.run_label = cf.run_label
    dec.run_score = cf.run_score
    dec.path_efficiency = cf.path_efficiency
    dec.chop_index = cf.chop_index
    dec.is_runner = cf.is_runner
    dec.is_choppy = cf.is_choppy
    return dec
