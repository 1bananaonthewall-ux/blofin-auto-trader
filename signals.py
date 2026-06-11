from __future__ import annotations

import logging
import importlib

from config import Settings
from exchange_client import BlofinExchange
from ml.predictor import MLPredictor
from strategy import Signal, StrategyDecision, evaluate_enhanced, _fee_aware_adjust
from strategy_10x30 import Signal as Signal10x30, StrategyDecision10x30, evaluate_10x30
from scalp_profile import profile_for
from ta_confluence import confluence_to_decision, run_all_analyses

from pick_engine import MLContext, evaluate_pick_for_symbol
from winner_gate import WinnerVerdict, evaluate_winner
from llm_policy import decide_with_llm
from hourly_3r import hourly_3r_active, is_entry_starved, is_opens_starved

log = logging.getLogger(__name__)
_OVERRIDE_MOD = None


def _analyze_llm_only(
    settings: Settings,
    symbol: str,
    ohlcv_1m,
    ohlcv_5m,
    funding: float | None,
    *,
    equity: float | None,
) -> StrategyDecision | None:
    """7B GGUF sole entry brain — no ML, winner, pick, markov, or swarm gates."""
    sp_prof = profile_for(settings)
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

    cf = run_all_analyses(ohlcv_1m, ohlcv_5m, funding_rate=funding, ml_decision=None)
    if cf is None:
        return None

    baseline = confluence_to_decision(cf)
    llm_dec = decide_with_llm(
        symbol=symbol,
        close=baseline.close,
        baseline=baseline,
        confluence_score=cf.confluence_score,
        agreeing=len(cf.agreeing),
        opposing=len(cf.opposing),
        funding_rate=funding,
        markov_state=None,
        markov_stress_p=None,
        min_confidence=settings.llm_trading_min_confidence,
        max_tokens=settings.llm_trading_max_tokens,
        temperature=settings.llm_trading_temperature,
        fail_open=False,
        use_cortex=settings.llm_trading_use_cortex,
        strict=True,
        respect_markov=False,
        equity=equity,
        state_dir=settings.state_dir,
        cache_sec=settings.llm_policy_cache_sec,
        llm_only=True,
    )
    if llm_dec is None or llm_dec.signal == Signal.FLAT:
        return None

    decision = llm_dec
    conf = decision.model_confidence
    if conf + 1e-4 < settings.llm_trading_min_confidence:
        return None
    if decision.score < settings.llm_trading_min_score:
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
        tp = sp * sp_prof.min_rr
    from tpsl_policy import align_stop_take, resolve_tpsl_policy

    pol = resolve_tpsl_policy(settings, decision=decision)
    sp, tp, pol = align_stop_take(settings, sp, tp, estimated_lev, decision=decision, style=pol.style)
    if sp <= 0 or tp / max(sp, 1e-9) < pol.min_rr * 0.98:
        return None

    decision.stop_pct = sp
    decision.take_pct = tp
    decision.trade_style = pol.style
    decision.leveraged_rr = tp / max(sp, 0.001) * estimated_lev
    decision.funding_rate = funding
    decision.winner_tier = "apex"
    decision.winner_score = conf
    decision.pick_score = conf
    decision.fast_win_score = conf

    log.info(
        "LLM-ONLY %s %s conf=%.2f score=%.0f rr=%.2f:1 zone=%s",
        symbol,
        decision.signal.value,
        conf,
        decision.score,
        tp / max(sp, 1e-9),
        getattr(decision, "confluence_zone", ""),
    )
    return decision


def _apply_optimizer_overrides(
    conf_gate: float,
    score_gate: float,
    *,
    markov_state: str = "",
    trades_last_hour: int = 0,
) -> tuple[float, float]:
    global _OVERRIDE_MOD
    try:
        if _OVERRIDE_MOD is None:
            _OVERRIDE_MOD = importlib.import_module("optimizer_overrides")
        else:
            _OVERRIDE_MOD = importlib.reload(_OVERRIDE_MOD)
        if hasattr(_OVERRIDE_MOD, "apply_overrides"):
            cg, sg = _OVERRIDE_MOD.apply_overrides(
                conf_gate, score_gate, markov_state=markov_state, trades_last_hour=trades_last_hour
            )
            return float(cg), float(sg)
    except Exception as exc:
        log.debug("optimizer_overrides skipped: %s", exc)
    return conf_gate, score_gate


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
    if getattr(settings, "llm_overseer_mode", False):
        from llm_overseer import get_gate_adjustments, symbol_avoided

        cd, sd = get_gate_adjustments(settings.state_dir)
        conf_gate += cd
        score_gate += sd
        if symbol_avoided(symbol, settings.state_dir):
            return None
    ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", 100)
    ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", 50)
    funding = ex.fetch_funding_rate(symbol)

    if getattr(settings, "llm_only_trading", False) and not getattr(
        settings, "llm_overseer_mode", False
    ):
        return _analyze_llm_only(
            settings, symbol, ohlcv_1m, ohlcv_5m, funding, equity=equity
        )

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
    bump_score = sp_prof.min_signal_score_bump if sp_prof else 0.0
    bump_conf = sp_prof.min_confidence_bump if sp_prof else 0.0
    if hourly_3r_active(settings) and is_entry_starved(settings):
        bump_score *= 0.35
        bump_conf *= 0.35
    if hourly_3r_active(settings) and is_opens_starved(settings):
        bump_conf *= 0.5
        bump_score *= 0.5
    try:
        from account_guard import universe_fill_active

        if universe_fill_active(settings):
            bump_conf *= 0.35
            bump_score *= 0.35
    except Exception:
        pass
    if equity > 0 and equity < getattr(settings, "micro_equity_threshold", 10.0) * 2:
        bump_conf *= 0.4
        bump_score *= 0.4
    score_gate = score_gate + bump_score
    conf_gate = conf_gate + bump_conf
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

    opens_starved = hourly_3r_active(settings) and is_opens_starved(settings)
    entry_starved = hourly_3r_active(settings) and is_entry_starved(settings)
    throughput_relax = not quality_first and (opens_starved or entry_starved)
    min_confluence = 0.48 if throughput_relax else 0.52
    min_agreeing = 4 if throughput_relax else 5

    cf = run_all_analyses(
        ohlcv_1m,
        ohlcv_5m,
        funding_rate=funding,
        ml_decision=ml_decision,
        min_confluence_score=min_confluence,
        min_agreeing_votes=min_agreeing,
    )
    if cf is None:
        return None

    rq = None
    if getattr(settings, "runner_filter_enabled", True):
        from run_quality import measure_run_quality

        rq = measure_run_quality(
            ohlcv_1m,
            ohlcv_5m,
            min_runner_score=getattr(settings, "runner_min_score", 0.48),
            max_chop=getattr(settings, "runner_max_chop", 0.56),
            min_path_eff=getattr(settings, "runner_min_path_eff", 0.26),
        )
        if rq:
            cf.run_label = rq.label
            cf.run_score = rq.runner_score
            cf.path_efficiency = rq.path_efficiency_1m
            cf.chop_index = rq.chop_index
            cf.is_runner = rq.is_runner
            cf.is_choppy = rq.is_choppy
            if rq.is_runner and cf.regime == "ranging":
                cf.regime = "trending"
            if cf.is_choppy and not throughput_relax:
                return None
            if not cf.is_runner and not throughput_relax:
                return None
            elite_floor = getattr(settings, "runner_min_score", 0.48) + (0.0 if throughput_relax else 0.02)
            if rq.runner_score < elite_floor and not throughput_relax:
                return None

    mk_snap = None
    if settings.markov_regime_enabled:
        from markov_regime import get_markov_engine

        mk_snap = get_markov_engine(settings.state_dir).update(symbol, ohlcv_1m)
        if mk_snap:
            if mk_snap.state == "trend":
                conf_gate = max(0.35, conf_gate - settings.markov_confidence_boost_trend)
                score_gate = max(45.0, score_gate - 1.5)
            elif mk_snap.state == "stress":
                stress_conf = settings.markov_confidence_penalty_stress
                stress_score = 3.0
                if not quality_first and hourly_3r_active(settings) and is_opens_starved(settings):
                    stress_conf *= 0.45
                    stress_score *= 0.5
                conf_gate = min(0.96, conf_gate + stress_conf)
                score_gate = min(99.0, score_gate + stress_score)
    tph = 0
    try:
        from scalp_optimizer import get_active_tuning

        tph = int(getattr(get_active_tuning(), "trades_last_hour", 0) or 0)
    except Exception:
        pass
    conf_gate, score_gate = _apply_optimizer_overrides(
        conf_gate,
        score_gate,
        markov_state=(mk_snap.state if mk_snap else ""),
        trades_last_hour=tph,
    )

    decision = confluence_to_decision(cf)
    if decision.signal == Signal.FLAT or decision.signal != cf.direction:
        return None
    conf = decision.model_confidence
    used_llm = False
    llm_only = bool(getattr(settings, "llm_only_trading", False)) and not getattr(
        settings, "llm_overseer_mode", False
    )
    if (
        settings.llm_trading_enabled
        and not getattr(settings, "llm_overseer_mode", False)
        and (llm_only or not hourly_3r_active(settings))
    ):
        pre_conf = 0.0 if llm_only else max(0.40, settings.llm_trading_min_confidence - 0.10)
        pre_score = 0.0 if llm_only else max(40.0, settings.llm_trading_min_score - 10.0)
        fail_open = False if llm_only else settings.llm_trading_fail_open
        strict = True if llm_only else settings.llm_trading_strict
        should_call = llm_only or (conf >= pre_conf and decision.score >= pre_score)
        if should_call:
            llm_dec = decide_with_llm(
                symbol=symbol,
                close=decision.close,
                baseline=decision,
                confluence_score=cf.confluence_score,
                agreeing=len(cf.agreeing),
                opposing=len(cf.opposing),
                funding_rate=funding,
                markov_state=(mk_snap.state if mk_snap else None),
                markov_stress_p=(mk_snap.probs[2] if mk_snap else None),
                min_confidence=settings.llm_trading_min_confidence,
                max_tokens=settings.llm_trading_max_tokens,
                temperature=settings.llm_trading_temperature,
                fail_open=fail_open,
                use_cortex=settings.llm_trading_use_cortex,
                strict=strict,
                respect_markov=False if llm_only else settings.llm_trading_respect_markov,
                equity=equity,
                state_dir=settings.state_dir,
                cache_sec=settings.llm_policy_cache_sec,
            )
            if llm_dec is not None:
                decision = llm_dec
                conf = decision.model_confidence
                used_llm = True
                log.info(
                    "LLM POLICY %s %s conf=%.2f score=%.0f zone=%s%s",
                    symbol,
                    decision.signal.value,
                    conf,
                    decision.score,
                    getattr(decision, "confluence_zone", ""),
                    " (llm_only)" if llm_only else "",
                )
        elif llm_only or not fail_open:
            return None

    if llm_only and not used_llm:
        return None
    if llm_only and decision.signal == Signal.FLAT:
        return None

    if used_llm:
        post_conf_gate = settings.llm_trading_min_confidence
        post_score_gate = settings.llm_trading_min_score
    else:
        post_conf_gate = conf_gate
        post_score_gate = score_gate

    try:
        from quality_pick import apply_quality_gates, quality_pick_active

        if quality_pick_active(settings) or getattr(settings, "entries_never_pause", False):
            post_conf_gate, post_score_gate = apply_quality_gates(
                settings, post_conf_gate, post_score_gate
            )
        else:
            from account_guard import universe_fill_active

            if universe_fill_active(settings):
                micro = equity is not None and equity > 0 and equity < settings.micro_equity_threshold
                if micro:
                    post_conf_gate = min(post_conf_gate, 0.52)
                    post_score_gate = min(post_score_gate, 52.0)
                elif hourly_3r_active(settings) and is_opens_starved(settings):
                    post_conf_gate = min(post_conf_gate, 0.52)
                    post_score_gate = min(post_score_gate, 52.0)
    except Exception:
        pass

    if conf + 1e-4 < post_conf_gate:
        log.info(
            "GATE skip %s: conf %.2f < %.2f%s",
            symbol,
            conf,
            post_conf_gate,
            " (llm)" if used_llm else "",
        )
        return None
    if decision.score < post_score_gate:
        log.info(
            "GATE skip %s: score %.0f < %.0f%s",
            symbol,
            decision.score,
            post_score_gate,
            " (llm)" if used_llm else "",
        )
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

    from liquidation_guard import max_safe_stop_pct
    from runner_momentum import (
        extended_runner_take_pct,
        is_directional_runner,
        runner_priority_active,
    )

    runner_priority = runner_priority_active(settings)
    if runner_priority or (
        getattr(settings, "momentum_wave_mode", False)
        and not hourly_3r_active(settings)
        and not (sp_prof and sp_prof.three_r_mode)
    ):
        target_lev_gain = max(0.10, float(settings.momentum_wave_target_levered_profit_pct) / 100.0)
        min_tp = max(min_tp, target_lev_gain / max(float(estimated_lev), 1.0))

    regime = decision.regime
    if mk_snap and mk_snap.state == "stress":
        regime = "volatile"
    elif mk_snap and mk_snap.state == "trend":
        regime = "trending"

    sp, tp, _ = _fee_aware_adjust(
        decision.stop_pct,
        decision.take_pct,
        equity,
        settings.small_account_threshold,
        min_tp,
        (settings.fee_est_taker_pct + settings.fee_est_maker_pct) * 100,
        model_confidence=conf,
        leverage=estimated_lev,
        regime=regime,
        min_rr_override=sp_prof.min_rr if sp_prof and sp_prof.three_r_mode else None,
    )
    if mk_snap:
        sp_cap = sp_prof.max_stop_pct if sp_prof else 0.12
        sp = min(sp_cap, sp * mk_snap.stop_mult)
        if sp_prof and sp_prof.three_r_mode:
            tp = sp * sp_prof.min_rr
        else:
            tp = tp * (2.0 - mk_snap.stop_mult * 0.5)
        decision.markov_state = mk_snap.state
        decision.markov_stress_p = mk_snap.probs[2]
    if sp_prof:
        sp = min(sp_prof.max_stop_pct, sp)
        tp = min(sp_prof.max_take_pct, max(tp, sp * sp_prof.min_rr))
    cf_runner = is_directional_runner(decision, settings)
    trade_style = None
    if sp_prof and sp_prof.three_r_mode:
        tp = sp * sp_prof.min_rr
        # Hourly/fast 3R mode keeps exchange 3:1 brackets — momentum runner TP (2.5R) fights winner gate.
        allow_runner_tp = runner_priority and cf_runner and not hourly_3r_active(settings)
        if allow_runner_tp:
            trade_style = "momentum"
            ext = extended_runner_take_pct(sp, settings=settings, leverage=estimated_lev)
            tp = min(ext, max(tp, sp * float(settings.runner_extend_min_rr)))
            sp = min(sp, max_safe_stop_pct(estimated_lev, settings=settings))
            log.info(
                "RUNNER TP %s: stop=%.2f%% take=%.2f%% (momentum, capped stop)",
                symbol.split("/")[0],
                sp * 100,
                tp * 100,
            )
        elif tp > sp_prof.max_take_pct:
            log.debug("3R skip %s: take %.2f%% exceeds cap", symbol, tp * 100)
            return None
        else:
            from tpsl_policy import resolve_tpsl_policy

            pol = resolve_tpsl_policy(settings, decision=decision)
            trade_style = "fast_3r" if hourly_3r_active(settings) else pol.style
            if hourly_3r_active(settings):
                tp = max(tp, sp * sp_prof.min_rr)
    from tpsl_policy import align_stop_take

    sp, tp, pol = align_stop_take(
        settings, sp, tp, estimated_lev, decision=decision, style=trade_style
    )
    if sp <= 0 or tp / max(sp, 1e-9) < pol.min_rr * 0.98:
        return None
    decision.stop_pct = sp
    decision.take_pct = tp
    decision.trade_style = pol.style
    decision.leveraged_rr = tp / max(sp, 0.001) * estimated_lev
    decision.funding_rate = funding

    if llm_only and used_llm:
        verdict = WinnerVerdict(True, "apex", max(0.9, conf), "llm_only_brain")
    else:
        verdict = evaluate_winner(
            decision,
            cf,
            settings,
            symbol=symbol,
            ml_decision=ml_decision,
            ml_ready=ml_ready,
            ml_ctx=ml_ctx,
        )
        if not verdict.ok:
            log.info("WINNER skip %s: %s", symbol, verdict.reason)
            return None
    if (
        not llm_only
        and getattr(settings, "runner_require_for_entry", False)
        and not cf_runner
    ):
        log.info(
            "RUNNER skip %s: runner-only mode (run_score=%.2f)",
            symbol,
            getattr(cf, "run_score", 0),
        )
        return None
    decision.winner_tier = verdict.tier
    decision.winner_score = verdict.score

    if llm_only and used_llm:
        from pick_engine import PickVerdict

        pick = PickVerdict(True, verdict.score, "llm_only_brain")
    else:
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
        "CONFLUENCE %s %s score=%.0f conf=%.2f cf=%.0f%% run=%s %.0f%% path=%.0f%% "
        "zone=[%s] agree=%d oppose=%d lev=%dx rr=%.2f:1",
        symbol,
        decision.signal.value,
        decision.score,
        conf,
        cf.confluence_score * 100,
        getattr(decision, "run_label", "mixed"),
        getattr(decision, "run_score", 0.5) * 100,
        getattr(decision, "path_efficiency", 0.5) * 100,
        getattr(decision, "confluence_zone", ""),
        len(cf.agreeing),
        len(cf.opposing),
        estimated_lev,
        tp / max(sp, 1e-9),
    )

    if mk_snap and mk_snap.state == "stress" and mk_snap.probs[2] > 0.55:
        stress_p = mk_snap.probs[2]
        allow_stress = (
            hourly_3r_active(settings)
            and sp_prof
            and sp_prof.three_r_mode
            and is_entry_starved(settings)
            and stress_p < 0.68
            and len(cf.agreeing) >= 5
            and len(cf.opposing) <= 2
        )
        if not allow_stress:
            log.info("MARKOV skip %s: stress belief %.0f%%", symbol, stress_p * 100)
            return None

    if settings.moon_swarm_enabled:
        from scalp_optimizer import get_active_tuning
        from swarm_brain import get_swarm_brain

        tuning = get_active_tuning()
        starved_swarm = is_entry_starved(settings, tuning)
        swarm = get_swarm_brain().consensus(
            decision,
            cf,
            settings,
            ml_ctx=ml_ctx,
            winner_tier=verdict.tier,
            pick_score=pick.score,
            starved=starved_swarm,
        )
        if not swarm.ok:
            bypass = (
                starved_swarm
                and getattr(decision, "confluence_zone", "") == "llm"
                and verdict.tier in ("apex", "elite")
            )
            if bypass:
                log.info("SWARM bypass %s (starved llm %s): %s", symbol, verdict.tier, swarm.reason)
            else:
                log.info("SWARM skip %s: %s", symbol, swarm.reason)
                return None
        decision.swarm_confidence = swarm.confidence
        decision.swarm_summary = swarm.summary

    return decision
