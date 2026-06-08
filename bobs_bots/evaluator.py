"""Evaluate one bar using live God Bot TA (ta_confluence + runner filter)."""

from __future__ import annotations

from typing import Any

from bobs_bots.regime import htf_adx, htf_ema_bias, signal_allowed
from bobs_bots.specs import BotSpec
from indicators import ema
from run_quality import measure_run_quality
from strategy import Signal, StrategyDecision
from ta_confluence import confluence_to_decision, run_all_analyses


def _apply_scalp_ctx(spec: BotSpec) -> None:
    run_all_analyses._scalp_ctx = {
        "atr_stop_mult": spec.atr_stop_mult,
        "atr_take_mult": spec.atr_take_mult,
        "max_stop_pct": spec.max_stop_pct,
        "max_take_pct": spec.max_take_pct,
        "min_rr": spec.min_rr,
        "three_r_mode": spec.three_r_mode,
    }


def evaluate_entry(
    ohlcv_fast: list[list[float]],
    ohlcv_htf: list[list[float]],
    spec: BotSpec,
    *,
    funding_rate: float | None = None,
    period_bias: str = "neutral",
    ohlcv_1h: list[list[float]] | None = None,
) -> StrategyDecision | None:
    """Backtest uses 5m fast + 1H HTF; live bot passes 1m + 5m into the same slots."""
    if len(ohlcv_fast) < 40:
        return None

    _apply_scalp_ctx(spec)
    cf = run_all_analyses(
        ohlcv_fast,
        ohlcv_htf,
        funding_rate=funding_rate,
        ml_decision=None,
        min_confluence_score=spec.min_confluence,
        min_agreeing_votes=spec.min_agreeing,
    )
    if cf is None:
        return None

    if spec.runner_filter:
        rq = measure_run_quality(
            ohlcv_fast,
            ohlcv_htf,
            min_runner_score=spec.min_runner_score,
            max_chop=spec.max_chop,
            min_path_eff=spec.min_path_eff,
        )
        if rq:
            cf.is_runner = rq.is_runner
            cf.is_choppy = rq.is_choppy
            cf.run_label = rq.label
            cf.run_score = rq.runner_score
            cf.path_efficiency = rq.path_efficiency_1m
            cf.chop_index = rq.chop_index
            if rq.is_runner and cf.regime == "ranging":
                cf.regime = "trending"
            if spec.skip_choppy and cf.is_choppy:
                return None
            if spec.require_runner and not cf.is_runner:
                return None

    decision = confluence_to_decision(cf)
    conf = decision.model_confidence or (decision.score / 100.0)
    if decision.score < spec.min_composite_score:
        return None
    if conf < spec.min_confidence:
        return None
    if decision.signal == Signal.FLAT:
        return None
    if decision.stop_pct <= 0 or decision.take_pct <= 0:
        return None

    closes_1h = [r[4] for r in ohlcv_1h] if ohlcv_1h else []
    htf_bias = htf_ema_bias(closes_1h) if closes_1h else None
    adx_v = htf_adx(ohlcv_1h or [])

    if not signal_allowed(
        decision.signal,
        htf_bias=htf_bias,
        period=period_bias,
        require_htf_align=spec.require_htf_align,
        trend_with_period=spec.trend_with_period,
        trend_only=spec.trend_only,
        htf_aligned=bool(decision.htf_aligned),
        min_adx=spec.min_adx_1h,
        adx_val=adx_v,
    ):
        return None

    closes_fast = [r[4] for r in ohlcv_fast]
    ema21 = ema(closes_fast, 21)
    if ema21 and ema21[-1] is not None:
        px = closes_fast[-1]
        e = ema21[-1]
        side = decision.signal.value
        band = spec.pullback_band
        if side == "long" and px > e * (1 + band):
            return None
        if side == "short" and px < e * (1 - band):
            return None

    return decision


def decision_summary(dec: StrategyDecision) -> dict[str, Any]:
    return {
        "signal": dec.signal.value,
        "score": round(dec.score, 1),
        "stop_pct": round(dec.stop_pct, 5),
        "take_pct": round(dec.take_pct, 5),
        "confluence": round(getattr(dec, "confluence_score", 0), 3),
        "run_label": getattr(dec, "run_label", ""),
    }
