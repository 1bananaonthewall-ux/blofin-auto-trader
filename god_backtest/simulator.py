"""God Bot bar-walk backtest on cached candles."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from bobs_bots.evaluator import evaluate_entry
from bobs_bots.regime import rolling_period_bias
from bobs_bots.specs import BotSpec, get_spec
from god_backtest.candle_cache import load_symbol_candles, merge_ws_tail

log = logging.getLogger(__name__)

WARMUP_5M = 120
LOOKBACK_5M = 72
LOOKBACK_1H = 60


def _slice_htf(candles_1h: list[list[float]], ts: float) -> list[list[float]]:
    return [c for c in candles_1h if c[0] <= ts][-LOOKBACK_1H:]


def _metrics(trades: list[dict[str, Any]], starting_pot: float, curve: list[dict]) -> dict[str, Any]:
    if not curve:
        return {"error": "no_curve"}
    ending = curve[-1]["equity"]
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    gross_win = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (2.0 if gross_win > 0 else 1.0)
    peak = starting_pot
    max_dd = 0.0
    for pt in curve:
        e = pt["equity"]
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    ret = ((ending - starting_pot) / starting_pot * 100.0) if starting_pot else 0.0
    return {
        "ending_equity": round(ending, 2),
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades),
        "wins": wins,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(min(5.0, pf), 2),
    }


def backtest_symbol_window(
    spec: BotSpec,
    *,
    inst_id: str,
    starting_pot: float,
    start_ms: int,
    end_ms: int,
    ws_tail: dict[str, list[list[float]]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    candles_5m, candles_1h = load_symbol_candles(
        inst_id, start_ms=start_ms, end_ms=end_ms, use_cache=use_cache
    )
    if ws_tail:
        t5 = (ws_tail.get("5m") or []) if isinstance(ws_tail, dict) else []
        t1 = (ws_tail.get("1H") or []) if isinstance(ws_tail, dict) else []
        candles_5m = merge_ws_tail(inst_id, "5m", candles_5m, t5)
        candles_1h = merge_ws_tail(inst_id, "1H", candles_1h, t1)

    if len(candles_5m) < WARMUP_5M + 20:
        return {"error": "insufficient_5m", "bars": len(candles_5m)}

    equity = starting_pot
    position: dict[str, Any] | None = None
    last_entry_i = -spec.entry_gap_bars - 1
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, float]] = [{"ts": candles_5m[0][0], "equity": equity}]

    for i in range(WARMUP_5M, len(candles_5m)):
        bar = candles_5m[i]
        ts, _o, high, low, close = bar[0], bar[1], bar[2], bar[3], bar[4]
        if ts < start_ms or ts > end_ms:
            continue

        if position:
            side = position["side"]
            risk_usd = position["risk_usd"]
            hit = None
            if side == "long":
                if low <= position["stop"]:
                    hit = "loss"
                elif high >= position["take"]:
                    hit = "win"
            else:
                if high >= position["stop"]:
                    hit = "loss"
                elif low <= position["take"]:
                    hit = "win"
            if hit:
                fee = equity * (spec.fee_roundtrip_pct / 100.0)
                rr = position.get("rr", spec.min_rr)
                pnl = (risk_usd * rr - fee) if hit == "win" else (-risk_usd - fee)
                equity += pnl
                trades.append({"ts": ts, "side": side, "result": hit, "pnl_usd": round(pnl, 4)})
                position = None
                curve.append({"ts": ts, "equity": round(equity, 2)})
            continue

        if i - last_entry_i < spec.entry_gap_bars:
            continue

        window_5m = candles_5m[max(0, i - LOOKBACK_5M + 1) : i + 1]
        window_1h = _slice_htf(candles_1h, ts)
        bar_bias = rolling_period_bias(candles_1h, ts, start_ms=start_ms)
        dec = evaluate_entry(
            window_5m,
            window_1h,
            spec,
            period_bias=bar_bias,
            ohlcv_1h=window_1h,
        )
        if dec is None:
            continue

        risk_usd = equity * spec.risk_per_trade
        side = dec.signal.value
        entry = close
        rr = dec.take_pct / max(dec.stop_pct, 1e-9)
        if side == "long":
            stop = entry * (1 - dec.stop_pct)
            take = entry * (1 + dec.take_pct)
        else:
            stop = entry * (1 + dec.stop_pct)
            take = entry * (1 - dec.take_pct)

        position = {
            "side": side,
            "entry": entry,
            "stop": stop,
            "take": take,
            "risk_usd": risk_usd,
            "rr": rr,
        }
        last_entry_i = i

    stats = _metrics(trades, starting_pot, curve)
    if stats.get("error"):
        return stats
    return {"inst_id": inst_id, **stats}


def score_aggregate(rows: list[dict[str, Any]]) -> float:
    """Higher = better forward performance (return × quality, penalize drawdown)."""
    if not rows:
        return -1e9
    total_ret = sum(r.get("return_pct", 0) for r in rows)
    avg_pf = sum(r.get("profit_factor", 1) for r in rows) / len(rows)
    avg_dd = sum(r.get("max_drawdown_pct", 0) for r in rows) / len(rows)
    trades = sum(r.get("trades", 0) for r in rows)
    quality = total_ret * avg_pf / (1.0 + avg_dd / 100.0)
    if trades < 1:
        return -50.0
    if trades < 5:
        return quality * (trades / 5.0)
    return quality


def spec_from_params(base_id: str = "god-bot", **overrides: Any) -> BotSpec:
    spec = get_spec(base_id)
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(spec, **clean) if clean else spec
