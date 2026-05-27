from __future__ import annotations

import logging

from config import Settings
from exchange_client import BlofinExchange
from ml.predictor import MLPredictor
from strategy import Signal, StrategyDecision, evaluate_enhanced, _fee_aware_adjust
from strategy_10x30 import Signal as Signal10x30, StrategyDecision10x30, evaluate_10x30
from scalp_profile import profile_for
from ta_confluence import confluence_to_decision, run_all_analyses

from pick_engine import MLContext, evaluate_pick_for_symbol
from winner_gate import evaluate_winner

log = logging.getLogger(__name__)


def _estimate_leverage_from_confidence(confidence: float, settings: Settings) -> int:
    from risk import SmartPositionSizer

    sizer = SmartPositionSizer(
        equity=1000,
        max_leverage=settings.auto_leverage_max,
        base_leverage=settings.leverage,
        model_confidence=confidence,
        profit_factor=1.0,
    )
    return sizer.effective_leverage()


def _convert_10x30_to_standard(dec: StrategyDecision10x30, settings: Settings | None = None) -> StrategyDecision:
    from strategy import StrategyDecision as StdDecision

    confidence = dec.score / 100.0
    estimated_lev = _estimate_leverage_from_confidence(confidence, settings) if settings else 10

    return StdDecision(
        signal=Signal.LONG if dec.signal == Signal10x30.LONG else (Signal.SHORT if dec.signal == Signal10x30.SHORT else Signal.FLAT),
        score=dec.score,
        fast_ema=dec.close,
        slow_ema=dec.close,
        rsi=50.0,
        close=dec.close,
        stop_pct=dec.stop_pct,
        take_pct=dec.take_pct,
        volume_ratio=dec.volume_ratio,
        htf_aligned=True,
        funding_rate=dec.funding_rate,
        model_confidence=confidence,
        leveraged_rr=dec.reward_risk_ratio * estimated_lev,
    )


def analyze_symbol(
    ex: BlofinExchange,
    settings: Settings,
    symbol: str,
    ml: MLPredictor | None = None,
    equity: float | None = None,
    min_confidence: float | None = None,
    min_signal_score: float | None = None,
) -> StrategyDecision | None:
    conf_gate = min_confidence if min_confidence is not None else settings.ml_min_confidence
    score_gate = min_signal_score if min_signal_score is not None else settings.min_signal_score
    ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", 100)
    ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", 50)
    funding = ex.fetch_funding_rate(symbol)

    if settings.signal_mode == "10x30":
        dec = evaluate_10x30(ohlcv_1m, ohlcv_5m, funding_rate=funding)
        if dec is not None and dec.signal != Signal10x30.FLAT and dec.score >= score_gate:
            return _convert_10x30_to_standard(dec, settings=settings)
        return None

    ml_decision = None
    ml_ready = False
    ml_ctx = MLContext(ready=False)
    if settings.signal_mode == "ml" and ml is not None and ml.is_ready():
        ml_ready = True
        ml_decision = ml.predict(ohlcv_1m, ohlcv_5m, funding_rate=funding)
        pair = ml.predict_proba_pair(ohlcv_1m, ohlcv_5m, funding_rate=funding)
        base = ml_ctx
        if ml.model and ml.model.metrics:
            base = MLContext(
                ready=True,
                long_precision=ml.model.metrics.val_long_precision,
                short_precision=ml.model.metrics.val_short_precision,
            )
        if pair:
            p_long, p_short = pair
            sig = ml_decision.signal if ml_decision else Signal.FLAT
            conf = ml_decision.model_confidence if ml_decision else max(p_long, p_short)
            ml_ctx = MLContext(
                ready=True,
                p_long=p_long,
                p_short=p_short,
                long_precision=base.long_precision,
                short_precision=base.short_precision,
                signal=sig,
                confidence=conf,
            )

    sp_prof = profile_for(settings)
    score_gate = score_gate + (sp_prof.min_signal_score_bump if sp_prof else 0.0)
    conf_gate = conf_gate + (sp_prof.min_confidence_bump if sp_prof else 0.0)
    if sp_prof:
        run_all_analyses._scalp_ctx = {
            "atr_stop_mult": sp_prof.atr_stop_mult,
            "atr_take_mult": sp_prof.atr_take_mult,
            "max_stop_pct": sp_prof.max_stop_pct,
            "max_take_pct": sp_prof.max_take_pct,
            "min_rr": sp_prof.min_rr,
            "three_r_mode": sp_prof.three_r_mode,
        }
    else:
        run_all_analyses._scalp_ctx = None

    cf = run_all_analyses(ohlcv_1m, ohlcv_5m, funding_rate=funding, ml_decision=ml_decision)
    if cf is None:
        return None

    decision = confluence_to_decision(cf)
    conf = decision.model_confidence

    if conf < conf_gate:
        return None
    if decision.score < score_gate:
        return None

    if sp_prof:
        estimated_lev = max(
            sp_prof.base_leverage,
            min(
                sp_prof.max_leverage_cap,
                int(sp_prof.base_leverage + (sp_prof.max_leverage_cap - sp_prof.base_leverage) * conf),
            ),
        )
        min_tp = sp_prof.min_take_profit_pct
    else:
        estimated_lev = _estimate_leverage_from_confidence(conf, settings)
        min_tp = settings.min_take_profit_pct

    sp, tp, _ = _fee_aware_adjust(
        decision.stop_pct,
        decision.take_pct,
        equity,
        settings.small_account_threshold,
        min_tp,
        (settings.fee_est_taker_pct + settings.fee_est_maker_pct) * 100,
        model_confidence=conf,
        leverage=estimated_lev,
        regime=decision.regime,
        min_rr_override=sp_prof.min_rr if sp_prof and sp_prof.three_r_mode else None,
    )
    if sp_prof:
        sp = min(sp_prof.max_stop_pct, sp)
        tp = min(sp_prof.max_take_pct, max(tp, sp * sp_prof.min_rr))
    if sp_prof and sp_prof.three_r_mode:
        tp = sp * sp_prof.min_rr
        if tp > sp_prof.max_take_pct:
            log.debug("3R skip %s: take %.2f%% exceeds cap", symbol, tp * 100)
            return None
    if sp <= 0 or tp / max(sp, 1e-9) < (sp_prof.min_rr if sp_prof else 1.25) * 0.98:
        return None
    decision.stop_pct = sp
    decision.take_pct = tp
    decision.leveraged_rr = tp / max(sp, 0.001) * estimated_lev
    decision.funding_rate = funding

    verdict = evaluate_winner(
        decision,
        cf,
        settings,
        ml_decision=ml_decision,
        ml_ready=ml_ready,
        ml_ctx=ml_ctx,
    )
    if not verdict.ok:
        log.info("WINNER skip %s: %s", symbol, verdict.reason)
        return None
    decision.winner_tier = verdict.tier
    decision.winner_score = verdict.score

    pick = evaluate_pick_for_symbol(
        symbol,
        decision,
        cf,
        settings,
        ml_ctx=ml_ctx,
        winner_score=verdict.score,
        winner_tier=verdict.tier,
    )
    if not pick.ok:
        log.info("PICK skip %s: %s", symbol, pick.reason)
        return None
    decision.winner_score = max(verdict.score, pick.score)
    decision.pick_score = pick.score
    decision.fast_win_score = pick.fast_win

    log.info(
        "CONFLUENCE %s %s score=%.0f conf=%.2f cf=%.0f%% zone=[%s] agree=%d oppose=%d lev=%dx rr=%.2f:1",
        symbol,
        decision.signal.value,
        decision.score,
        conf,
        cf.confluence_score * 100,
        getattr(decision, "confluence_zone", ""),
        len(cf.agreeing),
        len(cf.opposing),
        estimated_lev,
        tp / max(sp, 1e-9),
    )
    return decision
