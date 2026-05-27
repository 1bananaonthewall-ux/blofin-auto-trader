"""Rank setups by conviction; allow multiple opens only when tied at the top."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from strategy import StrategyDecision

# Must be nearly tied to the #1 conviction to open in the same cycle
TIE_ABS_GAP = 0.022
TIE_REL_GAP = 0.035
MIN_CONVICTION_FOR_ENTRY = 0.52

@dataclass
class RankedSetup:
    symbol: str
    decision: StrategyDecision
    conviction: float
    confidence: float
    score: float


def conviction_score(decision: StrategyDecision, path_reliability: float) -> float:
    """Higher = stronger edge — driven by TA confluence + ML + path reliability."""
    conf = getattr(decision, "model_confidence", 0.0) or (decision.score / 100.0)
    cf = getattr(decision, "confluence_score", 0.0) or conf
    score_norm = min(1.0, decision.score / 100.0)
    margin = abs(conf - 0.5) * 2.0
    agree_n = getattr(decision, "confluence_agreeing", 0)
    agree_boost = min(1.15, 1.0 + agree_n * 0.02)
    rr_bonus = min(1.2, getattr(decision, "leveraged_rr", 1.0) / 10.0) if hasattr(decision, "leveraged_rr") else 1.0
    tier = getattr(decision, "winner_tier", "")
    winner_boost = (
        1.12
        if tier == "apex"
        else (1.08 if tier == "elite" else (1.03 if tier == "good" else 1.0))
    )
    # Don't zero-out ranking when fluid path_reliability is low — winner gate already filtered quality
    rel = max(0.72, path_reliability)
    conv = cf * score_norm * rel * (0.85 + 0.15 * margin) * agree_boost * rr_bonus * winner_boost
    ws = getattr(decision, "winner_score", 0.0)
    ps = getattr(decision, "pick_score", 0.0) or ws
    if tier == "apex" and ps > 0:
        conv = max(conv, ps * 0.96)
    elif tier == "elite" and ps > 0:
        conv = max(conv, ps * 0.92)
    elif tier == "good" and ps > 0:
        conv = max(conv, ps * 0.78)
    return conv


def rank_setups(
    candidates: list[tuple[str, StrategyDecision]],
    path_reliability: float,
    *,
    mission_scale: Callable[[float], float] | None = None,
) -> list[RankedSetup]:
    ranked: list[RankedSetup] = []
    for sym, dec in candidates:
        conf = getattr(dec, "model_confidence", 0.0) or (dec.score / 100.0)
        conv = conviction_score(dec, path_reliability)
        if mission_scale:
            conv = mission_scale(conv)
        ranked.append(
            RankedSetup(symbol=sym, decision=dec, conviction=conv, confidence=conf, score=dec.score)
        )
    ranked.sort(
        key=lambda r: (
            getattr(r.decision, "pick_score", 0.0),
            getattr(r.decision, "winner_score", 0.0),
            r.conviction,
            r.confidence,
            r.score,
        ),
        reverse=True,
    )
    return ranked


def _tier_pool(
    ranked: list[RankedSetup],
    *,
    apex_preferred: bool,
    elite_only: bool,
    allow_elite_fallback: bool,
) -> list[RankedSetup]:
    """Prefer apex stacks; fall back to elite when starved if allowed."""
    apex = [r for r in ranked if getattr(r.decision, "winner_tier", "") == "apex"]
    elite = [r for r in ranked if getattr(r.decision, "winner_tier", "") == "elite"]
    if apex_preferred and apex:
        return apex
    if apex_preferred and not allow_elite_fallback:
        return []
    if elite_only or apex_preferred:
        return elite if elite else []
    return ranked


def select_conviction_ties(
    ranked: list[RankedSetup],
    *,
    max_opens: int = 3,
    abs_gap: float = TIE_ABS_GAP,
    rel_gap: float = TIE_REL_GAP,
    min_conviction: float = MIN_CONVICTION_FOR_ENTRY,
    apex_preferred: bool = False,
    elite_only: bool = False,
    allow_elite_fallback: bool = True,
) -> list[RankedSetup]:
    """
    Return setups eligible this cycle: #1 plus any others damn-near tied for top.
    Stops at first non-tie — no machine-gun on weaker signals.
    """
    if not ranked or max_opens <= 0:
        return []
    pool = _tier_pool(
        ranked,
        apex_preferred=apex_preferred,
        elite_only=elite_only,
        allow_elite_fallback=allow_elite_fallback,
    )
    if not pool:
        return []
    top = pool[0].conviction
    tier = getattr(pool[0].decision, "winner_tier", "")
    floor = min_conviction
    if tier == "apex":
        floor = max(floor, 0.55)
    elif tier in ("elite", "good"):
        floor = max(floor, 0.48)
    if top < floor:
        return []

    elite = [pool[0]]
    for r in pool[1:max_opens]:
        gap = top - r.conviction
        rel = gap / top if top > 0 else 1.0
        if gap <= abs_gap or rel <= rel_gap:
            elite.append(r)
        else:
            break
    return elite


def margin_fraction_for_conviction(
    conviction: float,
    confidence: float,
    *,
    base_pct: float,
    max_pct: float,
    action_intensity: float,
    tie_count: int = 1,
) -> float:
    """
    Share of *current* free margin for one setup (caller splits when multiple ties).
    """
    t = conviction * (0.6 + 0.4 * confidence) * action_intensity
    t = max(0.0, min(1.0, t))
    raw = base_pct + (max_pct - base_pct) * t
    if tie_count > 1:
        # Split deploy budget across tied elites; slight haircut for correlation risk
        return raw / tie_count * 0.92
    return raw
