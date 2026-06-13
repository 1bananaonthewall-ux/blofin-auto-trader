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

REGIME_MAX_CHASE = {
    "trending": 0.008,
    "climbing": 0.010,
    "ranging": 0.003,
    "choppy": 0.002,
    "mixed": 0.005,
    "volatile": 0.004,
}

REGIME_MIN_PICK = {
    "trending": 0.52,
    "climbing": 0.55,
    "ranging": 0.62,
    "choppy": 0.65,
    "mixed": 0.55,
    "volatile": 0.60,
}


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

    run_s = getattr(cf, "run_score", 0.5)
    if getattr(cf, "is_runner", False):
        s += 0.22
        tags.append("runner")
    elif run_s >= 0.55:
        s += 0.08
        tags.append("run_bias")
    elif getattr(cf, "is_choppy", False):
        s -= 0.14
        tags.append("choppy_penalty")
    elif run_s < 0.40:
        s -= 0.06

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

    from hourly_3r import hourly_3r_active, is_entry_starved

    opp_ratio = _weighted_opposition_ratio(cf, side)
    hard_opp = settings.winner_max_opposition_ratio + 0.14
    starved_3r = hourly_3r_active(settings) and is_entry_starved(settings)
    from account_guard import universe_fill_active

    universe = universe_fill_active(settings)
    never_loosen = getattr(settings, "entries_never_pause", False)
    try:
        from quality_pick import quality_pick_active

        never_loosen = never_loosen or quality_pick_active(settings)
    except Exception:
        pass
    if not never_loosen and (starved_3r or universe):
        hard_opp += 0.14
    if opp_ratio > hard_opp:
        return PickVerdict(False, winner_score, f"opposition {opp_ratio:.0%} overwhelming")

    # Volatility gate — skip extreme ATR / wide spread
    atr_pct = getattr(cf, "atr_pct", 0.0)
    spread_pct = getattr(cf, "spread_pct", 0.0)
    max_atr = getattr(settings, "max_atr_pct", 0.025)
    max_spread = getattr(settings, "max_spread_pct", 0.0015)
    if atr_pct > max_atr:
        return PickVerdict(False, winner_score, f"vol gate ATR {atr_pct:.1%}")
    if spread_pct > max_spread:
        return PickVerdict(False, winner_score, f"vol gate spread {spread_pct:.3%}")

    # Session hour gate
    try:
        from winner_intel import session_hour_blocked

        blocked, reason = session_hour_blocked(settings.state_dir)
        if blocked and winner_tier not in ("elite", "apex"):
            return PickVerdict(False, winner_score, f"session block {reason}")
    except Exception:
        pass

    # Pullback wait — reject chase unless elite/apex + ML tailwind
    from forward_pick import ml_direction_edge

    ml_edge = ml_direction_edge(ml_ctx, side)
    vd = cf.vwap_distance_pct
    chase = abs(vd)
    max_chase = REGIME_MAX_CHASE.get(cf.regime, 0.005)
    if chase > max_chase:
        bypass = winner_tier in ("elite", "apex") and ml_edge > 0.12
        if not bypass:
            return PickVerdict(
                False,
                winner_score,
                f"chase {chase:.2%} > {max_chase:.2%} — wait pullback",
            )

    # 1H HTF structure confirmation
    htf_1h = getattr(cf, "htf_1h_aligned", getattr(cf, "htf_15m_aligned", cf.htf_aligned))
    if not htf_1h and winner_tier not in ("elite", "apex"):
        if ml_edge < 0.10:
            return PickVerdict(
                False,
                winner_score,
                f"1h HTF misaligned — need ML edge (have {ml_edge:.2f})",
            )

    # 15m HTF structure confirmation
    htf_15m = getattr(cf, "htf_15m_aligned", cf.htf_aligned)
    if not htf_15m and winner_tier not in ("elite", "apex"):
        if ml_edge < 0.08:
            return PickVerdict(
                False,
                winner_score,
                f"15m HTF misaligned — need ML edge (have {ml_edge:.2f})",
            )

    # Quality chop block
    quality_mode = False
    try:
        from quality_pick import quality_pick_active

        quality_mode = quality_pick_active(settings)
    except Exception:
        pass
    chop = getattr(cf, "chop_index", 0.5)
    path_eff = getattr(cf, "path_efficiency", 0.5)
    is_choppy_setup = getattr(cf, "is_choppy", False) or (
        chop >= 0.50 and path_eff < 0.30
    )
    if quality_mode and is_choppy_setup:
        if winner_tier not in ("elite", "apex"):
            return PickVerdict(
                False,
                winner_score,
                f"quality chop block chop={chop:.0%} path={path_eff:.0%}",
            )
        if ml_edge < 0.10:
            return PickVerdict(
                False,
                winner_score,
                f"quality chop — need ML edge (have {ml_edge:.2f})",
            )

    from runner_momentum import runner_priority_active

    runner_priority = runner_priority_active(settings)
    if getattr(settings, "runner_filter_enabled", True):
        chop = getattr(cf, "chop_index", 0.5)
        path_eff = getattr(cf, "path_efficiency", 0.5)
        max_chop = getattr(settings, "runner_max_chop", 0.56)
        min_path = getattr(settings, "runner_min_path_eff", 0.26)
        min_run = getattr(settings, "runner_min_score", 0.48)
        is_runner = getattr(cf, "is_runner", False) or getattr(cf, "run_score", 0.5) >= min_run + 0.04
        if getattr(cf, "is_choppy", False) or (chop >= max_chop and path_eff < min_path):
            if not (starved_3r and not never_loosen) and not (runner_priority and is_runner):
                return PickVerdict(
                    False,
                    winner_score,
                    f"choppy {chop:.0%} path={path_eff:.0%} — need directional runner",
                )
        if getattr(cf, "run_score", 0.5) < min_run - 0.14 and path_eff < min_path - 0.04:
            if not (starved_3r and not never_loosen):
                return PickVerdict(
                    False,
                    winner_score,
                    f"weak runner score {getattr(cf, 'run_score', 0):.2f}",
                )

    if _rsi_exhausted(side, decision.rsi):
        rsi_bypass = (
            winner_tier in ("good", "elite", "apex")
            and len(cf.opposing) <= 3
            and (
                (starved_3r and not never_loosen)
                or (universe and not never_loosen)
                or len(cf.agreeing) >= 4
                or winner_score >= getattr(settings, "winner_min_score", 0.55) - 0.04
                or winner_tier in ("elite", "apex")
            )
        )
        if not rsi_bypass:
            return PickVerdict(False, winner_score, f"RSI exhausted ({decision.rsi:.0f})")

    sym_wr, sym_n = _recent_side_edge(settings.state_dir, symbol, side.value)
    cold_floor = 0.30 if never_loosen else 0.22
    if sym_wr is not None and sym_wr < cold_floor:
        return PickVerdict(
            False, winner_score, f"{symbol} {side.value} cold streak wr={sym_wr:.0%} n={sym_n}",
        )

    fast, tags = _fast_win_score(decision, cf, side, ml_ctx)
    trend, trend_tags = _trend_horizon_score(decision, cf, side, ml_ctx)
    w_short = getattr(settings, "pick_short_horizon_weight", 0.55)
    w_short = max(0.35, min(0.65, w_short))
    fused = w_short * fast + (1.0 - w_short) * trend
    tier_boost = 0.08 if winner_tier == "apex" else (0.05 if winner_tier == "elite" else 0.02)
    pick = min(1.0, winner_score * 0.28 + fused * 0.72 + tier_boost)

    from forward_pick import forward_pick_adjustments

    fwd_boost, floor_cut = forward_pick_adjustments(
        state_dir=settings.state_dir,
        symbol=symbol,
        side=side,
        ml_ctx=ml_ctx,
        fast_win=fast,
        winner_tier=winner_tier,
    )
    pick = min(1.0, pick + fwd_boost)

    if sym_wr is not None and sym_wr >= 0.55:
        pick = min(1.0, pick + 0.04)

    from winner_intel import regime_floor_adjustment

    base_regime_floor = REGIME_MIN_PICK.get(cf.regime, 0.55)
    regime_floor = regime_floor_adjustment(settings.state_dir, cf.regime, base_regime_floor)
    min_pick = max(getattr(settings, "pick_min_score", 0.62), regime_floor)
    from quality_pick import quality_pick_active

    if getattr(settings, "llm_overseer_mode", False):
        try:
            from llm_overseer import get_winner_adjustments, overseer_min_winner_tier, symbol_avoided

            if symbol_avoided(symbol, settings.state_dir):
                return PickVerdict(False, pick, f"overseer avoid {symbol.split('/')[0]}")
            od = get_winner_adjustments(settings.state_dir)
            if od.pick_min_delta > 0:
                min_pick = max(min_pick, getattr(settings, "pick_min_score", 0.62) + od.pick_min_delta)
            tier_rank = {"good": 0, "elite": 1, "apex": 2}
            floor = overseer_min_winner_tier(settings.state_dir)
            if tier_rank.get(winner_tier, 0) < tier_rank.get(floor, 0):
                return PickVerdict(
                    False,
                    pick,
                    f"overseer tier {winner_tier} < floor {floor}",
                    fast_win=fast,
                )
        except Exception:
            pass

    quality_mode = quality_pick_active(settings)
    try:
        starved = is_entry_starved(settings)
    except Exception:
        starved = False
    never_loosen = quality_mode or getattr(settings, "entries_never_pause", False)
    ml_forward_strong = fwd_boost >= 0.06 or floor_cut >= 0.04
    wr, _pf = (0.5, 1.0)
    if never_loosen:
        try:
            from quality_pick import live_performance

            wr, _pf = live_performance(settings)
        except Exception:
            pass
        if wr < 0.42:
            min_pick = max(min_pick, 0.58)
        elif wr < 0.48:
            min_pick = max(min_pick, 0.55)
        if winner_tier not in ("elite", "apex"):
            if not (starved and ml_forward_strong):
                min_pick = max(min_pick, 0.52)
        if starved and ml_forward_strong:
            starved_cap = 0.46 if hourly_3r_active(settings) else 0.50
            min_pick = min(min_pick, starved_cap)
    elif starved:
        min_pick = min(min_pick, 0.42 if hourly_3r_active(settings) else 0.48)
    min_pick = max(0.48, min_pick - floor_cut)

    if never_loosen and ml_ctx.ready and ml_edge < -0.06:
        return PickVerdict(
            False,
            pick,
            f"ML headwind edge={ml_edge:.2f}",
            fast_win=fast,
        )
    if never_loosen and wr < 0.45:
        if not ml_ctx.ready or ml_edge < 0.05:
            return PickVerdict(
                False,
                pick,
                f"weak live WR — need ML tailwind (edge={ml_edge:.2f})",
                fast_win=fast,
            )
        if fast < 0.48:
            return PickVerdict(
                False,
                pick,
                f"weak live WR — fast_win {fast:.2f} < 0.48",
                fast_win=fast,
            )

    if pick < min_pick:
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
    # 1m candle-close confirmation
    try:
        from winner_intel import candle_close_confirmed

        ohlcv_probe = getattr(settings, "_entry_ohlcv_1m", None)
        ok_candle, why_candle = candle_close_confirmed(
            ohlcv_probe,
            side.value,
            fast_ema=decision.fast_ema,
        )
        if not ok_candle and winner_tier not in ("elite", "apex"):
            bypass = ml_edge > 0.10
            if not bypass:
                return PickVerdict(False, winner_score, f"candle gate {why_candle}", fast_win=fast)
    except Exception:
        pass

    if ml_ctx.ready and side == Signal.SHORT and ml_ctx.short_precision < 0.42:
        if ml_ctx.p_short < ml_ctx.p_long + 0.14:
            return PickVerdict(
                False,
                pick,
                f"short OOS weak p={ml_ctx.short_precision:.0%} ml edge insufficient",
                fast_win=fast,
            )

    reason = f"fused={fused:.2f} fast={fast:.2f} trend={trend:.2f} " + "+".join((tags + trend_tags)[:4])
    if fwd_boost > 0 or floor_cut > 0:
        reason += f" fwd=+{fwd_boost:.2f} floor_cut={floor_cut:.2f}"
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
