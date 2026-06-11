"""
Cortex copilot — Qwen vets top-ranked setups after ML/winner/pick.

Uses ever-learning local_cortex knowledge + mission/curve context from cortex_trader.
Does not replace the scan; only finalists get a policy call (cheap, cached).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from strategy import Signal

if TYPE_CHECKING:
    from config import Settings
    from conviction import RankedSetup

log = logging.getLogger(__name__)


def copilot_active(settings: "Settings") -> bool:
    if not getattr(settings, "llm_copilot_trading", False):
        return False
    if getattr(settings, "llm_only_trading", False):
        return False
    try:
        from local_llm import resolve_provider

        return resolve_provider() != "none"
    except Exception:
        return False


def vet_ranked_setups(
    settings: "Settings",
    ranked: list["RankedSetup"],
    *,
    max_vet: int,
    equity: float | None,
) -> list["RankedSetup"]:
    """Run cortex policy on top convictions; boost approvals, bury vetoes."""
    if not ranked or not copilot_active(settings):
        return ranked

    from llm_policy import decide_with_llm

    try:
        from local_cortex import train

        train(min_new_closes=0)
    except Exception:
        pass

    cap = max(1, min(int(max_vet), len(ranked)))
    pool = sorted(ranked, key=lambda r: r.conviction, reverse=True)[:cap]
    approved: list[str] = []
    vetoed = 0
    strict = bool(getattr(settings, "llm_copilot_strict", True))

    for r in pool:
        d = r.decision
        if d.signal == Signal.FLAT:
            continue
        llm_dec = decide_with_llm(
            symbol=r.symbol,
            close=float(d.close or 0),
            baseline=d,
            confluence_score=float(getattr(d, "confluence_score", 0) or d.score or 0),
            agreeing=int(getattr(d, "confluence_agreeing", 0) or 0),
            opposing=int(getattr(d, "confluence_opposing", 0) or 0),
            funding_rate=d.funding_rate,
            markov_state=getattr(d, "markov_state", None),
            markov_stress_p=getattr(d, "markov_stress_p", None),
            min_confidence=settings.llm_trading_min_confidence,
            max_tokens=settings.llm_trading_max_tokens,
            temperature=settings.llm_trading_temperature,
            fail_open=False,
            use_cortex=settings.llm_trading_use_cortex,
            strict=True,
            respect_markov=settings.llm_trading_respect_markov,
            equity=equity,
            state_dir=settings.state_dir,
            cache_sec=settings.llm_policy_cache_sec,
        )
        sym = r.symbol.split("/")[0]
        if llm_dec is None or llm_dec.signal == Signal.FLAT:
            vetoed += 1
            r.conviction *= 0.08
            continue
        if llm_dec.signal != d.signal:
            vetoed += 1
            r.conviction *= 0.06
            log.info("CORTEX COPILOT veto %s: LLM %s vs setup %s", sym, llm_dec.signal.value, d.signal.value)
            continue
        conf = float(llm_dec.model_confidence or 0)
        r.decision = llm_dec
        r.confidence = conf
        r.conviction = min(1.0, r.conviction * 1.14 + conf * 0.10)
        approved.append(sym)

    ranked.sort(
        key=lambda r: (
            1 if getattr(r.decision, "confluence_zone", "") == "cortex_llm" else 0,
            r.conviction,
            r.confidence,
        ),
        reverse=True,
    )

    if strict and approved:
        for r in ranked:
            if getattr(r.decision, "confluence_zone", "") != "cortex_llm":
                r.conviction *= 0.12

    if approved or vetoed:
        log.warning(
            "CORTEX COPILOT vetted=%d approved=%s vetoed=%d strict=%s",
            cap,
            ",".join(approved) or "-",
            vetoed,
            strict,
        )
    return ranked
