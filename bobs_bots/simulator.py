"""Bar-walk backtest using real confluence decisions."""

from __future__ import annotations

import logging
import time
from typing import Any

from bobs_bots.data import clear_candle_cache, load_symbol_candles
from bobs_bots.evaluator import evaluate_entry
from bobs_bots.specs import BOT_SPECS, BotSpec, get_spec
from bobs_bots.period import resolve_backtest_range
from storefront_market import list_tradeable_assets

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
    losses = sum(1 for t in trades if t["pnl_usd"] <= 0)
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
        "starting_pot": starting_pot,
        "ending_equity": round(ending, 2),
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(min(5.0, pf), 2),
        "equity_curve": curve[-200:],
    }


def backtest_symbol(
    spec: BotSpec,
    *,
    inst_id: str,
    starting_pot: float,
    start_ms: int,
    end_ms: int,
    asset_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candles_5m, candles_1h = load_symbol_candles(inst_id, start_ms=start_ms, end_ms=end_ms)
    if len(candles_5m) < WARMUP_5M + 20:
        return {"error": "insufficient_5m", "bars": len(candles_5m)}

    equity = starting_pot
    peak = equity
    position: dict[str, Any] | None = None
    last_entry_i = -spec.entry_gap_bars - 1
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, float]] = [{"ts": candles_5m[0][0], "equity": equity}]

    for i in range(WARMUP_5M, len(candles_5m)):
        bar = candles_5m[i]
        ts, _o, high, low, close = bar[0], bar[1], bar[2], bar[3], bar[4]
        if ts < start_ms:
            continue

        if position:
            side = position["side"]
            risk_usd = position["risk_usd"]
            hit = None
            if side == "long":
                if low <= position["stop"]:
                    hit = "loss"
                    exit_px = position["stop"]
                elif high >= position["take"]:
                    hit = "win"
                    exit_px = position["take"]
            else:
                if high >= position["stop"]:
                    hit = "loss"
                    exit_px = position["stop"]
                elif low <= position["take"]:
                    hit = "win"
                    exit_px = position["take"]
            if hit:
                fee = equity * (spec.fee_roundtrip_pct / 100.0)
                rr = position.get("rr", spec.min_rr)
                if hit == "win":
                    pnl = risk_usd * rr - fee
                else:
                    pnl = -risk_usd - fee
                equity += pnl
                trades.append(
                    {
                        "ts": ts,
                        "side": side,
                        "result": hit,
                        "pnl_usd": round(pnl, 4),
                        "equity": round(equity, 2),
                    }
                )
                position = None
                peak = max(peak, equity)
                curve.append({"ts": ts, "equity": round(equity, 2)})
            continue

        if i - last_entry_i < spec.entry_gap_bars:
            continue

        window_5m = candles_5m[max(0, i - LOOKBACK_5M + 1) : i + 1]
        window_1h = _slice_htf(candles_1h, ts)
        window_1h_full = _slice_htf(candles_1h, ts)
        dec = evaluate_entry(
            window_5m,
            window_1h,
            spec,
            period_bias="neutral",
            ohlcv_1h=window_1h_full,
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
            "opened_ts": ts,
        }
        last_entry_i = i

    if position:
        last = candles_5m[-1][4]
        side = position["side"]
        risk_usd = position["risk_usd"]
        fee = equity * (spec.fee_roundtrip_pct / 100.0)
        if side == "long":
            pnl = ((last - position["entry"]) / position["entry"]) * equity * 0.5 - fee
        else:
            pnl = ((position["entry"] - last) / position["entry"]) * equity * 0.5 - fee
        equity += pnl
        trades.append({"ts": candles_5m[-1][0], "side": side, "result": "open_close", "pnl_usd": round(pnl, 4)})
        curve.append({"ts": candles_5m[-1][0], "equity": round(equity, 2)})

    meta = asset_meta or {}
    stats = _metrics(trades, starting_pot, curve)
    if stats.get("error"):
        return stats
    return {
        "inst_id": inst_id,
        "symbol": meta.get("symbol", inst_id),
        "base": meta.get("base", inst_id.replace("-USDT", "")),
        "tradingview": meta.get("tradingview", ""),
        "bot_id": spec.id,
        "bot_name": spec.name,
        **stats,
    }


def compare_bots(
    *,
    bot_ids: list[str] | None = None,
    starting_pot: float = 1000.0,
    start_date: str | None = None,
    end_date: str | None = None,
    inst_ids: list[str] | None = None,
    max_assets: int = 10,
) -> dict[str, Any]:
    period = resolve_backtest_range(start_date=start_date, end_date=end_date)
    ids = bot_ids or list(BOT_SPECS.keys())
    specs = [get_spec(b) for b in ids]
    all_assets = list_tradeable_assets()
    if inst_ids:
        assets = [a for a in all_assets if a["inst_id"] in inst_ids]
    else:
        assets = all_assets[:max_assets]

    clear_candle_cache()
    report: dict[str, Any] = {
        "period": period,
        "starting_pot": starting_pot,
        "assets": [a["inst_id"] for a in assets],
        "bots": {},
    }

    for spec in specs:
        rows: list[dict[str, Any]] = []
        errors = 0
        for asset in assets:
            try:
                row = backtest_symbol(
                    spec,
                    inst_id=asset["inst_id"],
                    starting_pot=starting_pot,
                    start_ms=period["start_ms"],
                    end_ms=period["end_ms"],
                    asset_meta=asset,
                )
                if row.get("error"):
                    errors += 1
                    continue
                rows.append(row)
            except Exception as exc:
                log.debug("%s %s failed: %s", spec.id, asset["inst_id"], exc)
                errors += 1
            time.sleep(0.04)
        rows.sort(key=lambda r: r.get("return_pct", 0), reverse=True)
        avg_ret = sum(r["return_pct"] for r in rows) / len(rows) if rows else 0.0
        avg_pf = sum(r["profit_factor"] for r in rows) / len(rows) if rows else 0.0
        avg_wr = sum(r["win_rate_pct"] for r in rows) / len(rows) if rows else 0.0
        total_trades = sum(r["trades"] for r in rows)
        report["bots"][spec.id] = {
            "name": spec.name,
            "description": spec.description,
            "assets_ok": len(rows),
            "assets_errors": errors,
            "avg_return_pct": round(avg_ret, 2),
            "avg_profit_factor": round(avg_pf, 2),
            "avg_win_rate_pct": round(avg_wr, 1),
            "total_trades": total_trades,
            "results": rows,
        }
    return report
