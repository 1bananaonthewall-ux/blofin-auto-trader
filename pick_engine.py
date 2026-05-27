"""
Entry-only pick intelligence — rank setups by how fast they should reach TP.

Same open rate (e.g. 2 slots); smarter *which* two get filled. Hold/SL/TP unchanged.
Hard-reject only clear losers; everything else scored for ranking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from strategy import Signal, StrategyDecision

if TYPE_CHECKING:
    from config import Settings
    from ta_confluence import ConfluenceResult

log = logging.getLogger(__name__)

MOMENTUM_VOTES = frozenset({"macd", "adx_trend", "volume", "structure", "ema_1m"})
ANCHOR_VOTES = frozenset({"htf_5m", "ml", "adx_trend", "structure", "ema_5m", "vwap"})


@dataclass(frozen=True)
class MLContext:
    ready: bool
    p_long: float = 0.0
    p_short: float = 0.0
    long_precision: float = 0.5
    short_precision: float = 0.0
    signal: Signal = Signal.FLAT
    confidence: float = 0.0


@dataclass(frozen=True)
class PickVerdict:
    ok: bool
    score: float
    reason: str
    fast_win: float = 0.0


def _weighted_opposition_ratio(cf: "ConfluenceResult", side: Signal) -> float:
    agree_w = lose_w = 0.0
    for v in cf.votes:
        if v.strength < 0.25:
            continue
        w = v.strength * v.weight
        if v.signal == side:
            agree_w += w
        elif v.signal != Signal.FLAT and v.signal != side:
            lose_w += w
    if agree_w <= 0:
        return 1.0
    return lose_w / agree_w


def _recent_side_edge(state_dir, symbol: str, side: str, *, window: int = 8) -> tuple[float | None, int]:
    path = state_dir / "trade_outcomes.jsonl"
    if not path.exists():
        return None, 0
    wins = total = 0
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()[-400:]):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            if row.get("symbol") != symbol or str(row.get("side", "")).lower() != side.lower():
                continue
            total += 1
            if row.get("outcome") == "win" or int(row.get("win", 0)) == 1:
                wins += 1
            if total >= window:
                break
    except Exception:
        return None, 0
    if total < 4:
        return None, total
    return wins / total, total


def _rsi_exhausted(side: Signal, rsi: float) -> bool:
    if side == Signal.LONG and rsi >= 74:
        return True
    if side == Signal.SHORT and rsi <= 26:
        return True
    return False


def _trend_horizon_score(
    decision: StrategyDecision,
    cf: "ConfluenceResult",
    side: Signal,
    ml_ctx: MLContext,
) -> tuple[float, list[str]]:
    """Long-horizon / HTF branch (MDPI attention on slower structure)."""
    tags: list[str] = []
    s = 0.0
    if cf.regime == "trending":
        s += 0.22
        tags.append("htf_trend")
    htf_votes = sum(1 for n in cf.agreeing if n in {"htf_5m", "ema_5m", "structure"})
    s += min(0.18, htf_votes * 0.06)
    if htf_votes >= 2:
        tags.append("htf_anchors")
    if ml_ctx.ready:
        prec = ml_ctx.long_precision if side == Signal.LONG else ml_ctx.short_precision
        edge = (ml_ctx.p_long - ml_ctx.p_short) if side == Signal.LONG else (ml_ctx.p_short - ml_ctx.p_long)
        if edge > 0.05 and prec >= 0.45:
            s += min(0.16, edge * prec)
            tags.append("ml_trend")
    if side == Signal.LONG and decision.rsi < 68:
        s += 0.06
    elif side == Signal.SHORT and decision.rsi > 32:
        s += 0.06
    return min(1.0, max(0.0, s)), tags


def _fast_win_score(
    decision: StrategyDecision,
    cf: "ConfluenceResult",
    side: Signal,
    ml_ctx: MLContext,
) -> tuple[float, list[str]]:
    """Higher = likely to reach TP sooner from this entry (timing + momentum)."""
    tags: list[str] = []
    s = 0.0

    mom = sum(1 for n in cf.agreeing if n in MOMENTUM_VOTES)
    s += min(0.18, mom * 0.045)
    if mom >= 3:
        tags.append("momentum")

    anchors = sum(1 for n in cf.agreeing if n in ANCHOR_VOTES)
    s += min(0.10, anchors * 0.03)
    if anchors >= 3:
        tags.append("anchors")

    vol = cf.volume_ratio
    if vol >= 1.5:
        s += 0.14
        tags.append("vol_spike")
    elif vol >= 1.15:
        s += 0.07

    rsi = decision.rsi
    if side == Signal.LONG and 42 <= rsi <= 64:
        s += 0.10
        tags.append("rsi_room")
    elif side == Signal.SHORT and 36 <= rsi <= 58:
        s += 0.10
        tags.append("rsi_room")

    if cf.regime == "trending":
        s += 0.12
        tags.append("trend")

    vd = cf.vwap_distance_pct
    chase = abs(vd)
    if side == Signal.LONG and -0.006 <= vd <= 0.008:
        s += 0.10
        tags.append("vwap_pullback")
    elif side == Signal.SHORT and -0.008 <= vd <= 0.006:
        s += 0.10
        tags.append("vwap_pullback")
    elif chase < 0.012:
        s += 0.04

    close, fast, slow = decision.close, decision.fast_ema, decision.slow_ema
    if close > 0 and fast > 0 and slow > 0:
        if side == Signal.LONG and close >= fast >= slow:
            s += 0.08
            tags.append("ema_stack")
        elif side == Signal.SHORT and close <= fast <= slow:
            s += 0.08
            tags.append("ema_stack")

    if ml_ctx.ready:
        if side == Signal.LONG:
            edge = ml_ctx.p_long - ml_ctx.p_short
        else:
            edge = ml_ctx.p_short - ml_ctx.p_long
        if edge > 0.08:
            s += min(0.14, edge * 0.35)
            tags.append("ml_tailwind")
        elif edge > 0:
            s += min(0.06, edge * 0.2)
        elif edge < -0.12:
            s -= 0.08

    if len(cf.opposing) == 0:
        s += 0.05
        tags.append("clean")

    return min(1.0, max(0.0, s)), tags


def evaluate_pick_for_symbol(
    symbol: str,
    decision: StrategyDecision,
    cf: "ConfluenceResult",
    settings: "Settings",
    *,
    ml_ctx: MLContext,
    winner_score: float,
    winner_tier: str,
) -> PickVerdict:
    side = decision.signal
    if side == Signal.FLAT:
        return PickVerdict(False, 0.0, "flat")

    opp_ratio = _weighted_opposition_ratio(cf, side)
    hard_opp = settings.winner_max_opposition_ratio + 0.14
    if opp_ratio > hard_opp:
        return PickVerdict(False, winner_score, f"opposition {opp_ratio:.0%} overwhelming")

    if _rsi_exhausted(side, decision.rsi):
        return PickVerdict(False, winner_score, f"RSI exhausted ({decision.rsi:.0f})")

    sym_wr, sym_n = _recent_side_edge(settings.state_dir, symbol, side.value)
    if sym_wr is not None and sym_wr < 0.22:
        return PickVerdict(
            False, winner_score, f"{symbol} {side.value} cold streak wr={sym_wr:.0%} n={sym_n}",
        )

    fast, tags = _fast_win_score(decision, cf, side, ml_ctx)
    trend, trend_tags = _trend_horizon_score(decision, cf, side, ml_ctx)
    w_short = getattr(settings, "pick_short_horizon_weight", 0.55)
    w_short = max(0.35, min(0.65, w_short))
    fused = w_short * fast + (1.0 - w_short) * trend
    tier_boost = 0.06 if winner_tier == "elite" else 0.02
    pick = min(1.0, winner_score * 0.32 + fused * 0.68 + tier_boost)

    if sym_wr is not None and sym_wr >= 0.55:
        pick = min(1.0, pick + 0.04)

    min_pick = getattr(settings, "pick_min_score", 0.62)
    if pick < min_pick and winner_tier != "elite":
        return PickVerdict(
            False,
            pick,
            f"pick {pick:.2f} < floor {min_pick:.2f}",
            fast_win=fast,
        )

    if ml_ctx.ready and side == Signal.LONG and ml_ctx.long_precision < 0.42:
        if ml_ctx.p_long < ml_ctx.p_short + 0.14:
            return PickVerdict(
                False,
                pick,
                f"long OOS weak p={ml_ctx.long_precision:.0%} ml edge insufficient",
                fast_win=fast,
            )

    reason = f"fused={fused:.2f} fast={fast:.2f} trend={trend:.2f} " + "+".join((tags + trend_tags)[:4])
    log.info(
        "PICK %s %s score=%.2f fast=%.2f tier=%s | %s",
        symbol,
        side.value,
        pick,
        fast,
        winner_tier,
        reason,
    )
    return PickVerdict(True, pick, reason, fast_win=fast)
