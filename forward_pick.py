"""Forward-learning pick boosts — rank setups ML + live outcomes favor."""

from __future__ import annotations

import json
from pathlib import Path

from pick_engine import MLContext
from strategy import Signal


def ml_direction_edge(ml_ctx: MLContext, side: Signal) -> float:
    if not ml_ctx.ready or side == Signal.FLAT:
        return 0.0
    if side == Signal.LONG:
        return float(ml_ctx.p_long) - float(ml_ctx.p_short)
    return float(ml_ctx.p_short) - float(ml_ctx.p_long)


def ml_side_precision(ml_ctx: MLContext, side: Signal) -> float:
    if not ml_ctx.ready or side == Signal.FLAT:
        return 0.5
    return float(ml_ctx.long_precision if side == Signal.LONG else ml_ctx.short_precision)


def symbol_forward_wr(
    state_dir: Path,
    symbol: str,
    side: str,
    *,
    window: int = 10,
    min_trades: int = 3,
) -> tuple[float | None, int]:
    path = state_dir / "trade_outcomes.jsonl"
    if not path.is_file():
        return None, 0
    wins = total = 0
    try:
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()[-600:]):
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
    if total < min_trades:
        return None, total
    return wins / total, total


def forward_pick_adjustments(
    *,
    state_dir: Path,
    symbol: str,
    side: Signal,
    ml_ctx: MLContext,
    fast_win: float,
    winner_tier: str,
) -> tuple[float, float]:
    """
    Returns (pick_score_boost, min_pick_reduction).
    Positive reduction lowers the pick floor for proven ML-aligned setups.
    """
    boost = 0.0
    floor_cut = 0.0
    edge = ml_direction_edge(ml_ctx, side)
    prec = ml_side_precision(ml_ctx, side)

    if ml_ctx.ready and edge > 0.06:
        boost += min(0.10, edge * prec * 0.55)
        if edge > 0.12 and prec >= 0.48:
            floor_cut += 0.03
        if edge > 0.18 and prec >= 0.52:
            boost += 0.04
            floor_cut += 0.02
    elif ml_ctx.ready and edge < -0.08:
        boost -= min(0.12, abs(edge) * 0.35)

    if fast_win >= 0.52:
        boost += min(0.06, (fast_win - 0.50) * 0.20)
    if fast_win >= 0.62:
        floor_cut += 0.02

    sym_wr, _sym_n = symbol_forward_wr(state_dir, symbol, side.value if side != Signal.FLAT else "")
    boost += symbol_forward_boost(state_dir, symbol, side.value if side != Signal.FLAT else "")

    if sym_wr is not None:
        if sym_wr >= 0.55:
            boost += 0.05
            floor_cut += 0.03
        elif sym_wr >= 0.42:
            boost += 0.02

    if winner_tier == "apex":
        boost += 0.04
    elif winner_tier == "elite":
        boost += 0.02

    return round(boost, 4), round(min(0.08, floor_cut), 4)


def profitability_edge_ok(
    ml_ctx: MLContext,
    side: Signal,
    *,
    min_edge: float = 0.05,
) -> tuple[bool, float]:
    edge = ml_direction_edge(ml_ctx, side)
    if not ml_ctx.ready:
        return False, edge
    return edge >= min_edge, edge


def symbol_forward_blocked(
    state_dir: Path,
    symbol: str,
    side: str,
    *,
    cold_wr: float = 0.30,
    min_trades: int = 4,
) -> tuple[bool, str]:
    """Block symbol/side with poor forward win-rate feedback."""
    sym_wr, n = symbol_forward_wr(state_dir, symbol, side, min_trades=min_trades)
    if sym_wr is not None and n >= min_trades and sym_wr < cold_wr:
        return True, f"forward wr {sym_wr:.0%} n={n}"
    return False, ""


def symbol_forward_boost(
    state_dir: Path,
    symbol: str,
    side: str,
) -> float:
    """Auto-boost proven forward winners."""
    sym_wr, n = symbol_forward_wr(state_dir, symbol, side, min_trades=3)
    if sym_wr is None:
        return 0.0
    if sym_wr >= 0.65 and n >= 4:
        return 0.06
    if sym_wr >= 0.55 and n >= 3:
        return 0.03
    return 0.0
