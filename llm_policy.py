from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from local_llm import chat_completion, resolve_provider, status_line
from strategy import Signal, StrategyDecision

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
_POLICY_CACHE: dict[str, tuple[float, StrategyDecision]] = {}
_CACHE_SEC = float(os.environ.get("LLM_POLICY_CACHE_SEC", "45"))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _num(val: Any, default: float = 0.0) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            return default
    return default


def _safe_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s : e + 1])
    except Exception:
        return None
    return None


def _policy_system_prompt() -> str:
    return (
        "You are the PRIMARY live trading brain for a Blofin USDT perpetual scalper. "
        "Your sole objective is to steepen and hold the dashboard ACCOUNT CURVE vertical: "
        "maintain and exceed 10% account growth per day, harvest runners, avoid chop. "
        "You outperform generic bots by using MISSION_CONTEXT, baseline TA, run_label/path_efficiency, "
        "confluence, markov regime, funding, and cortex win/loss stats together. "
        "Return strict JSON only with keys: "
        'signal (long|short|flat), confidence (0..1), score (0..100), '
        "stop_pct (0.001..0.08), take_pct (0.001..0.25), reason (short). "
        "Rules: flat when uncertain; reward:risk >= 1.3 when not flat; "
        "in markov stress prefer flat unless confluence strongly agrees; "
        "never fight mission.entry_allowed=false — output flat; "
        "tighten stops when behind_schedule and preserve_capital."
    )


def decide_with_llm(
    *,
    symbol: str,
    close: float,
    baseline: StrategyDecision,
    confluence_score: float,
    agreeing: int,
    opposing: int,
    funding_rate: float | None,
    markov_state: str | None,
    markov_stress_p: float | None,
    min_confidence: float,
    max_tokens: int,
    temperature: float,
    fail_open: bool = True,
    use_cortex: bool = True,
    strict: bool = False,
    respect_markov: bool = True,
    equity: float | None = None,
    state_dir: Path | None = None,
    cache_sec: float | None = None,
) -> StrategyDecision | None:
    if resolve_provider() == "none":
        if fail_open:
            dec = baseline
            dec.confluence_zone = "llm_failopen_no_provider"
            return dec
        return None

    state_dir = state_dir or ROOT / "state"
    try:
        from cortex_trader import build_policy_context

        mission_ctx = build_policy_context(
            state_dir=state_dir, symbol=symbol, equity=equity
        )
    except Exception as exc:
        log.debug("policy context build: %s", exc)
        mission_ctx = {"symbol_focus": symbol}

    if mission_ctx.get("mission", {}).get("entry_allowed") is False and strict:
        return None

    cache_key = (
        f"{symbol}|{baseline.signal.value}|{round(float(baseline.model_confidence or 0), 2)}|"
        f"{int(round(confluence_score))}|{markov_state or ''}"
    )
    ttl = _CACHE_SEC if cache_sec is None else float(cache_sec)
    if ttl > 0:
        hit = _POLICY_CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

    user = {
        "symbol": symbol,
        "close": close,
        "llm_backend": status_line(),
        "funding_rate": funding_rate,
        "markov_state": markov_state or "",
        "markov_stress_p": float(markov_stress_p or 0.0),
        "MISSION_CONTEXT": mission_ctx,
        "baseline": {
            "signal": baseline.signal.value,
            "confidence": baseline.model_confidence,
            "score": baseline.score,
            "stop_pct": baseline.stop_pct,
            "take_pct": baseline.take_pct,
            "regime": baseline.regime,
            "vwap_distance_pct": baseline.vwap_distance_pct,
            "confluence_score": confluence_score,
            "agreeing": agreeing,
            "opposing": opposing,
            "run_label": getattr(baseline, "run_label", "mixed"),
            "run_score": getattr(baseline, "run_score", None),
            "path_efficiency": getattr(baseline, "path_efficiency", None),
            "chop_index": getattr(baseline, "chop_index", None),
            "is_runner": getattr(baseline, "is_runner", False),
            "is_choppy": getattr(baseline, "is_choppy", False),
            "pick_score": getattr(baseline, "pick_score", None),
        },
        "rules": [
            "You may override baseline direction when evidence supports it.",
            "Prefer flat when uncertain or run_label=choppy (whipsaw).",
            "Favor run_label=runner with high path_efficiency for account-curve growth.",
            "Keep confidence calibrated — do not inflate.",
            "Honor 3R: take_pct / stop_pct >= 1.3 when not flat.",
        ],
    }
    system = _policy_system_prompt()
    user_blob = json.dumps(user, separators=(",", ":"))
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    if use_cortex:
        try:
            from local_cortex import knowledge_block

            kb = knowledge_block(int(os.environ.get("LOCAL_CORTEX_POLICY_MAX_KNOWLEDGE_CHARS", "1400")))
            if kb:
                msgs.append({"role": "system", "content": f"CORTEX:\n{kb}"})
        except Exception:
            pass
    msgs.append({"role": "user", "content": user_blob})

    text, err = chat_completion(
        msgs, max_tokens=max_tokens, temperature=temperature, mode="policy"
    )
    if not text:
        if err:
            log.debug("LLM decision error %s %s", symbol, err)
        if fail_open:
            dec = baseline
            dec.confluence_zone = "llm_failopen_error"
            return dec
        return None
    blob = _safe_json(text)
    if not blob:
        log.debug("LLM non-JSON decision %s: %s", symbol, text[:160])
        if fail_open:
            dec = baseline
            dec.confluence_zone = "llm_failopen_nonjson"
            return dec
        return None

    sig_raw = str(blob.get("signal", "flat")).strip().lower()
    if sig_raw not in ("long", "short", "flat"):
        sig_raw = "flat"

    if sig_raw == "flat":
        if strict:
            return None
        if fail_open:
            sig_raw = baseline.signal.value if baseline.signal.value in ("long", "short") else "long"
            blob["signal"] = sig_raw
            blob["confidence"] = max(
                float(blob.get("confidence", 0.0) or 0.0),
                float(baseline.model_confidence or 0.0),
                min_confidence,
            )
            blob["score"] = max(float(blob.get("score", 0.0) or 0.0), float(baseline.score or 0.0))
        else:
            return None

    if respect_markov and (markov_state or "").lower() == "stress":
        stress_p = float(markov_stress_p or 0.0)
        if stress_p >= 0.52 and opposing > agreeing:
            if strict:
                return None
            sig_raw = baseline.signal.value if baseline.signal.value in ("long", "short") else "flat"
            if sig_raw == "flat" and not fail_open:
                return None

    conf_raw = _clamp(_num(blob.get("confidence"), baseline.model_confidence or 0.0), 0.0, 1.0)
    score_raw = _clamp(_num(blob.get("score"), baseline.score or 0.0), 0.0, 100.0)
    llm_weight = 0.78 if conf_raw >= 0.72 else 0.58
    conf = _clamp(
        llm_weight * conf_raw
        + (1.0 - llm_weight) * _clamp(float(baseline.model_confidence or 0.0), 0.0, 1.0)
        + 0.08 * _clamp(float(confluence_score) / 100.0, 0.0, 1.0),
        0.0,
        1.0,
    )
    score = _clamp(
        0.65 * score_raw + 0.35 * _clamp(float(confluence_score), 0.0, 100.0),
        0.0,
        100.0,
    )
    if conf < min_confidence:
        if fail_open:
            dec = baseline
            dec.confluence_zone = "llm_failopen_lowconf"
            return dec
        return None

    stop_pct = _clamp(_num(blob.get("stop_pct"), baseline.stop_pct), 0.001, 0.08)
    take_pct = _clamp(_num(blob.get("take_pct"), baseline.take_pct), 0.001, 0.25)
    rr = take_pct / max(stop_pct, 1e-9)
    if rr < 1.25:
        take_pct = stop_pct * 1.25

    reason = str(blob.get("reason") or "cortex_policy")[:120]
    log.info(
        "CORTEX POLICY %s %s conf=%.2f score=%.0f rr=%.2f reason=%s",
        symbol,
        sig_raw,
        conf,
        score,
        rr,
        reason,
    )

    dec = StrategyDecision(
        signal=Signal.LONG if sig_raw == "long" else Signal.SHORT,
        score=score,
        fast_ema=baseline.fast_ema,
        slow_ema=baseline.slow_ema,
        rsi=baseline.rsi,
        close=baseline.close,
        stop_pct=stop_pct,
        take_pct=take_pct,
        volume_ratio=baseline.volume_ratio,
        htf_aligned=baseline.htf_aligned,
        funding_rate=baseline.funding_rate,
        model_confidence=conf,
        leveraged_rr=baseline.leveraged_rr,
        regime=baseline.regime,
        vwap_distance_pct=baseline.vwap_distance_pct,
        confluence_score=baseline.confluence_score,
        confluence_zone="cortex_llm",
        confluence_agreeing=baseline.confluence_agreeing,
        confluence_opposing=baseline.confluence_opposing,
    )
    if ttl > 0:
        _POLICY_CACHE[cache_key] = (time.time(), dec)
        if len(_POLICY_CACHE) > 200:
            cutoff = time.time() - ttl * 2
            stale = [k for k, (ts, _) in _POLICY_CACHE.items() if ts < cutoff]
            for k in stale:
                _POLICY_CACHE.pop(k, None)
    return dec
