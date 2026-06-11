"""
Portfolio backtest for the growth agent — single equity pool, universe scan, max positions.

Uses the same TA confluence entry path as live God Bot (bobs_bots.evaluator).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any

from bobs_bots.evaluator import evaluate_entry
from bobs_bots.regime import rolling_period_bias
from bobs_bots.specs import BotSpec, get_spec
from god_backtest.candle_cache import load_symbol_candles
from god_backtest.simulator import score_aggregate

log = logging.getLogger(__name__)

WARMUP_5M = 120
LOOKBACK_5M = 72
LOOKBACK_1H = 60

# Grid tuned for OOS portfolio return (not per-symbol curve-fit).
GROWTH_PARAM_GRID: list[dict[str, Any]] = [
    {
        "min_confluence": 0.47,
        "min_composite_score": 47.0,
        "min_confidence": 0.55,
        "entry_gap_bars": 5,
        "risk_per_trade": 0.022,
        "skip_choppy": True,
        "scan_every_bars": 6,
    },
    {
        "min_confluence": 0.48,
        "min_composite_score": 48.0,
        "min_confidence": 0.56,
        "entry_gap_bars": 6,
        "risk_per_trade": 0.024,
        "skip_choppy": True,
        "scan_every_bars": 8,
    },
    {
        "min_confluence": 0.48,
        "min_composite_score": 49.0,
        "min_confidence": 0.57,
        "entry_gap_bars": 6,
        "risk_per_trade": 0.025,
        "skip_choppy": True,
        "scan_every_bars": 8,
    },
    {
        "min_confluence": 0.49,
        "min_composite_score": 50.0,
        "min_confidence": 0.58,
        "entry_gap_bars": 7,
        "risk_per_trade": 0.023,
        "skip_choppy": True,
        "scan_every_bars": 8,
    },
    {
        "min_confluence": 0.50,
        "min_composite_score": 51.0,
        "min_confidence": 0.59,
        "entry_gap_bars": 8,
        "risk_per_trade": 0.020,
        "skip_choppy": True,
        "scan_every_bars": 10,
    },
    {
        "min_confluence": 0.49,
        "min_composite_score": 49.0,
        "min_confidence": 0.57,
        "entry_gap_bars": 6,
        "risk_per_trade": 0.026,
        "skip_choppy": False,
        "scan_every_bars": 6,
    },
]

# Quality-focused grid: higher confluence / agreeing, always skip choppy.
GROWTH_LOGIC_GRID: list[dict[str, Any]] = GROWTH_PARAM_GRID + [
    {
        "min_confluence": 0.50,
        "min_composite_score": 50.0,
        "min_confidence": 0.58,
        "min_agreeing": 5,
        "entry_gap_bars": 6,
        "risk_per_trade": 0.022,
        "skip_choppy": True,
        "scan_every_bars": 8,
    },
    {
        "min_confluence": 0.48,
        "min_composite_score": 48.0,
        "min_confidence": 0.56,
        "min_agreeing": 5,
        "entry_gap_bars": 5,
        "risk_per_trade": 0.024,
        "skip_choppy": True,
        "scan_every_bars": 6,
    },
    {
        "min_confluence": 0.47,
        "min_composite_score": 47.0,
        "min_confidence": 0.55,
        "min_agreeing": 5,
        "entry_gap_bars": 5,
        "risk_per_trade": 0.022,
        "skip_choppy": True,
        "scan_every_bars": 6,
    },
    {
        "min_confluence": 0.49,
        "min_composite_score": 49.0,
        "min_confidence": 0.57,
        "min_agreeing": 5,
        "entry_gap_bars": 7,
        "risk_per_trade": 0.023,
        "skip_choppy": True,
        "scan_every_bars": 8,
    },
    {
        "min_confluence": 0.50,
        "min_composite_score": 51.0,
        "min_confidence": 0.59,
        "min_agreeing": 4,
        "entry_gap_bars": 8,
        "risk_per_trade": 0.020,
        "skip_choppy": True,
        "scan_every_bars": 10,
    },
    {
        "min_confluence": 0.48,
        "min_composite_score": 49.0,
        "min_confidence": 0.57,
        "min_agreeing": 4,
        "entry_gap_bars": 6,
        "risk_per_trade": 0.025,
        "skip_choppy": True,
        "scan_every_bars": 6,
    },
]


@dataclass(frozen=True)
class GrowthBtConfig:
    spec: BotSpec
    max_positions: int = 2
    scan_every_bars: int = 8
    max_daily_loss_pct: float = 15.0
    fee_roundtrip_pct: float = 0.05


@dataclass
class GrowthCandleUniverse:
    """Preloaded OHLCV — load once, reuse across grid trials."""

    symbols: list[str]
    c5: dict[str, list[list[float]]]
    c1h: dict[str, list[list[float]]]


def spec_from_growth_params(base_id: str = "god-bot", **overrides: Any) -> BotSpec:
    spec = get_spec(base_id)
    bt_only = {"scan_every_bars", "max_positions", "max_daily_loss_pct"}
    clean = {k: v for k, v in overrides.items() if v is not None and k not in bt_only}
    return replace(spec, **clean) if clean else spec


def _slice_window(candles: list[list[float]], end_i: int, lookback: int) -> list[list[float]]:
    start = max(0, end_i - lookback + 1)
    return candles[start : end_i + 1]


def _slice_htf(candles_1h: list[list[float]], ts: float) -> list[list[float]]:
    return [c for c in candles_1h if c[0] <= ts][-LOOKBACK_1H:]


def _metrics(trades: list[dict], starting_pot: float, curve: list[dict]) -> dict[str, Any]:
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
        "starting_pot": starting_pot,
        "ending_equity": round(ending, 2),
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades),
        "wins": wins,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(min(5.0, pf), 2),
    }


def _load_one_symbol(
    iid: str,
    *,
    start_ms: int,
    end_ms: int,
    use_cache: bool,
    ws_tails: dict[str, dict[str, list[list[float]]]] | None,
) -> tuple[str, list[list[float]], list[list[float]]] | None:
    from god_backtest.candle_cache import merge_ws_tail

    bars5, bars1h = load_symbol_candles(
        iid, start_ms=start_ms, end_ms=end_ms, use_cache=use_cache
    )
    tail = (ws_tails or {}).get(iid)
    if tail:
        bars5 = merge_ws_tail(iid, "5m", bars5, tail.get("5m") or [])
        bars1h = merge_ws_tail(iid, "1H", bars1h, tail.get("1H") or [])
    if len(bars5) < WARMUP_5M + 20:
        return None
    return iid, bars5, bars1h


def _load_universe(
    inst_ids: list[str],
    *,
    start_ms: int,
    end_ms: int,
    use_cache: bool = True,
    max_workers: int = 12,
    ws_tails: dict[str, dict[str, list[list[float]]]] | None = None,
) -> tuple[list[str], dict[str, list[list[float]]], dict[str, list[list[float]]]]:
    c5: dict[str, list[list[float]]] = {}
    c1h: dict[str, list[list[float]]] = {}
    ok: list[str] = []
    workers = max(1, min(max_workers, len(inst_ids) or 1))
    lock = threading.Lock()

    def _collect(row: tuple[str, list, list] | None) -> None:
        if row is None:
            return
        iid, bars5, bars1h = row
        with lock:
            c5[iid] = bars5
            c1h[iid] = bars1h
            ok.append(iid)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(
                _load_one_symbol,
                iid,
                start_ms=start_ms,
                end_ms=end_ms,
                use_cache=use_cache,
                ws_tails=ws_tails,
            )
            for iid in inst_ids
        ]
        for fut in as_completed(futs):
            try:
                _collect(fut.result())
            except Exception as exc:
                log.debug("load symbol failed: %s", exc)
    return ok, c5, c1h


def build_growth_universe(
    inst_ids: list[str],
    *,
    start_ms: int,
    end_ms: int,
    use_cache: bool = True,
    use_ws_tail: bool = True,
    max_workers: int = 12,
    ws_batch_size: int = 80,
    ws_timeout_sec: float = 8.0,
) -> GrowthCandleUniverse:
    """Parallel REST/cache load + batched WS tail merge (load once for grid search)."""
    t0 = __import__("time").time()
    ws_tails: dict[str, dict[str, list[list[float]]]] = {}
    if use_ws_tail and inst_ids:
        from god_backtest.ws_tail import sync_ws_tails_batched

        log.info("WS tail sync starting (%d symbols, batch=%d)", len(inst_ids), ws_batch_size)
        ws_tails = sync_ws_tails_batched(
            inst_ids,
            batch_size=ws_batch_size,
            timeout_sec=ws_timeout_sec,
        )

    symbols, c5, c1h = _load_universe(
        inst_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        use_cache=use_cache,
        max_workers=max_workers,
        ws_tails=ws_tails,
    )
    elapsed = round(__import__("time").time() - t0, 1)
    log.info(
        "growth universe ready: %d/%d symbols | ws=%d | %.1fs",
        len(symbols),
        len(inst_ids),
        len(ws_tails),
        elapsed,
    )
    return GrowthCandleUniverse(symbols=symbols, c5=c5, c1h=c1h)


def backtest_growth_portfolio(
    cfg: GrowthBtConfig,
    inst_ids: list[str],
    *,
    starting_pot: float,
    start_ms: int,
    end_ms: int,
    load_end_ms: int | None = None,
    use_cache: bool = True,
    universe: GrowthCandleUniverse | None = None,
    max_workers: int = 12,
    use_copilot: bool = False,
    copilot_strict: bool = True,
    copilot_memory: Any | None = None,
) -> dict[str, Any]:
    """
    Single-account walk: scan universe, rank confluence setups, 3R TP/SL on exchange model.
    use_copilot: walk-forward cortex veto on finalists (learns from prior closes only).
  """
    if use_copilot and copilot_memory is None:
        from god_backtest.copilot_memory import CortexMemory

        copilot_memory = CortexMemory()

    want = set(inst_ids)
    if universe is not None:
        symbols = [s for s in universe.symbols if s in want]
        c5 = {s: universe.c5[s] for s in symbols}
        c1h = {s: universe.c1h[s] for s in symbols}
    else:
        symbols, c5, c1h = _load_universe(
            inst_ids,
            start_ms=start_ms,
            end_ms=load_end_ms or end_ms,
            use_cache=use_cache,
            max_workers=max_workers,
        )
    if not symbols:
        return {"error": "no_symbols"}

    clock = c5[symbols[0]]
    ts_to_i: dict[str, dict[float, int]] = {}
    for iid in symbols:
        ts_to_i[iid] = {bar[0]: idx for idx, bar in enumerate(c5[iid])}

    spec = cfg.spec
    equity = starting_pot
    positions: dict[str, dict[str, Any]] = {}
    last_entry_i: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, float]] = []
    day_start: dict[str, float] = {}
    entries_paused = False

    for master_i in range(WARMUP_5M, len(clock)):
        ts = clock[master_i][0]
        if ts < start_ms or ts > end_ms:
            continue

        # --- manage open positions (exchange TP/SL only — no early ROE harvest) ---
        closed_this_bar: list[str] = []
        for iid, pos in list(positions.items()):
            idx = ts_to_i[iid].get(ts)
            if idx is None:
                continue
            bar = c5[iid][idx]
            _t, _o, high, low, _c = bar[0], bar[1], bar[2], bar[3], bar[4]
            side = pos["side"]
            hit = None
            if side == "long":
                if low <= pos["stop"]:
                    hit = "loss"
                elif high >= pos["take"]:
                    hit = "win"
            else:
                if high >= pos["stop"]:
                    hit = "loss"
                elif low <= pos["take"]:
                    hit = "win"
            if hit:
                fee = equity * (cfg.fee_roundtrip_pct / 100.0)
                rr = pos.get("rr", spec.min_rr)
                pnl = (pos["risk_usd"] * rr - fee) if hit == "win" else (-pos["risk_usd"] - fee)
                equity += pnl
                trades.append(
                    {
                        "ts": ts,
                        "inst_id": iid,
                        "side": side,
                        "result": hit,
                        "pnl_usd": round(pnl, 4),
                    }
                )
                closed_this_bar.append(iid)
        for iid in closed_this_bar:
            positions.pop(iid, None)

        curve.append({"ts": ts, "equity": round(equity, 2)})

        # Daily loss floor
        from datetime import datetime, timezone

        day_key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day_key not in day_start:
            day_start[day_key] = equity
        day_ret = (equity / day_start[day_key] - 1.0) * 100.0 if day_start[day_key] > 0 else 0.0
        entries_paused = day_ret <= -cfg.max_daily_loss_pct

        if master_i % cfg.scan_every_bars != 0 or entries_paused:
            continue
        if len(positions) >= cfg.max_positions:
            continue

        bar_bias = rolling_period_bias(c1h[symbols[0]], ts, start_ms=start_ms)
        candidates: list[tuple[str, Any, float]] = []

        for iid in symbols:
            if iid in positions:
                continue
            idx = ts_to_i[iid].get(ts)
            if idx is None or idx < WARMUP_5M:
                continue
            if idx - last_entry_i.get(iid, -spec.entry_gap_bars - 1) < spec.entry_gap_bars:
                continue
            window_5m = _slice_window(c5[iid], idx, LOOKBACK_5M)
            window_1h = _slice_htf(c1h[iid], ts)
            dec = evaluate_entry(
                window_5m,
                window_1h,
                spec,
                period_bias=bar_bias,
                ohlcv_1h=window_1h,
            )
            if dec is None:
                continue
            conf = dec.model_confidence or (dec.score / 100.0)
            rank = conf * (dec.score / 100.0)
            if dec.signal.value == "short":
                rank *= 1.02
            candidates.append((iid, dec, rank))

        candidates.sort(key=lambda x: x[2], reverse=True)
        slots = cfg.max_positions - len(positions)
        opened_this_scan = 0
        for iid, dec, _rank in candidates:
            if opened_this_scan >= slots or len(positions) >= cfg.max_positions:
                break
            if use_copilot and copilot_memory is not None:
                ok, _reason = copilot_memory.copilot_approve(
                    iid,
                    dec.signal.value,
                    dec,
                    strict=copilot_strict,
                )
                if not ok:
                    continue
            idx = ts_to_i[iid][ts]
            close = c5[iid][idx][4]
            side = dec.signal.value
            risk_usd = equity * spec.risk_per_trade
            rr = dec.take_pct / max(dec.stop_pct, 1e-9)
            if side == "long":
                stop = close * (1 - dec.stop_pct)
                take = close * (1 + dec.take_pct)
            else:
                stop = close * (1 + dec.stop_pct)
                take = close * (1 - dec.take_pct)
            positions[iid] = {
                "side": side,
                "entry": close,
                "stop": stop,
                "take": take,
                "risk_usd": risk_usd,
                "rr": rr,
            }
            last_entry_i[iid] = idx
            opened_this_scan += 1

    stats = _metrics(trades, starting_pot, curve)
    stats["symbols"] = len(symbols)
    stats["spec_id"] = spec.id
    if use_copilot and copilot_memory is not None:
        stats["copilot_vetoes"] = copilot_memory.vetoes
        stats["copilot_approvals"] = copilot_memory.approvals
        stats["copilot_closes_learned"] = copilot_memory.total_closes
    if len(trades) >= 10:
        mid = len(trades) // 2
        for label, chunk in ("early_half", trades[:mid]), ("late_half", trades[mid:]):
            wins = sum(1 for t in chunk if t["pnl_usd"] > 0)
            stats[label] = {
                "trades": len(chunk),
                "win_rate_pct": round(wins / len(chunk) * 100, 1),
                "pnl_usd": round(sum(t["pnl_usd"] for t in chunk), 2),
            }
    return stats


def portfolio_score(row: dict[str, Any]) -> float:
    """Higher = better — penalize drawdown, reward PF and return."""
    if row.get("error"):
        return -1e9
    ret = float(row.get("return_pct", 0))
    pf = float(row.get("profit_factor", 1))
    dd = float(row.get("max_drawdown_pct", 0))
    trades = int(row.get("trades", 0))
    if trades < 3:
        return ret * 0.2 - 20
    quality = ret * pf / (1.0 + dd / 80.0)
    if ret < 0:
        quality *= 0.5
    return quality


def portfolio_score_balanced(row: dict[str, Any]) -> float:
    """Favor win rate + PF + return — for logic tuning that must pick more winners."""
    if row.get("error"):
        return -1e9
    ret = float(row.get("return_pct", 0))
    pf = float(row.get("profit_factor", 1))
    dd = float(row.get("max_drawdown_pct", 0))
    wr = float(row.get("win_rate_pct", 0))
    trades = int(row.get("trades", 0))
    if trades < 15:
        return ret * 0.1 - 50.0
    if pf < 1.02:
        return ret * 0.15 - 40.0
    quality = (ret * 0.35 + wr * 1.8) * pf / (1.0 + dd / 70.0)
    if ret < 0:
        quality *= 0.25
    if wr < 22.0:
        quality *= 0.75
    return quality


def validate_holdout(
    best_params: dict[str, Any],
    inst_ids: list[str],
    *,
    starting_pot: float,
    train_end_ms: int,
    test_start_ms: int,
    test_end_ms: int,
    max_positions: int = 2,
    use_cache: bool = True,
    universe: GrowthCandleUniverse | None = None,
) -> dict[str, Any]:
    """Out-of-sample check: run best train params on the held-out window only."""
    raw = dict(best_params)
    scan_bars = int(raw.pop("scan_every_bars", 8))
    spec = spec_from_growth_params(**raw)
    cfg = GrowthBtConfig(
        spec=spec,
        max_positions=max_positions,
        scan_every_bars=scan_bars,
    )
    row = backtest_growth_portfolio(
        cfg,
        inst_ids,
        starting_pot=starting_pot,
        start_ms=test_start_ms,
        end_ms=test_end_ms,
        load_end_ms=test_end_ms,
        use_cache=use_cache,
        universe=universe,
    )
    row["holdout_score"] = round(portfolio_score(row), 4)
    row["holdout_days"] = round((test_end_ms - test_start_ms) / 86400000, 1)
    return row


def optimize_growth_params(
    inst_ids: list[str],
    *,
    starting_pot: float,
    start_ms: int,
    end_ms: int,
    load_end_ms: int | None = None,
    max_positions: int = 2,
    use_cache: bool = True,
    grid: list[dict[str, Any]] | None = None,
    score_fn=None,
    universe: GrowthCandleUniverse | None = None,
    use_ws_tail: bool = True,
    max_workers: int = 12,
) -> dict[str, Any]:
    """Grid search on portfolio backtest; returns best params + full report."""
    grid = grid or GROWTH_PARAM_GRID
    score_fn = score_fn or portfolio_score
    candle_end = load_end_ms or end_ms
    if universe is None:
        universe = build_growth_universe(
            inst_ids,
            start_ms=start_ms,
            end_ms=candle_end,
            use_cache=use_cache,
            use_ws_tail=use_ws_tail,
            max_workers=max_workers,
        )
    best_params: dict[str, Any] = {}
    best_score = -1e18
    best_row: dict[str, Any] = {}
    trials: list[dict[str, Any]] = []

    for raw in grid:
        params = dict(raw)
        scan_bars = int(params.pop("scan_every_bars", 8))
        spec = spec_from_growth_params(**params)
        cfg = GrowthBtConfig(
            spec=spec,
            max_positions=max_positions,
            scan_every_bars=scan_bars,
        )
        row = backtest_growth_portfolio(
            cfg,
            inst_ids,
            starting_pot=starting_pot,
            start_ms=start_ms,
            end_ms=end_ms,
            load_end_ms=candle_end,
            use_cache=use_cache,
            universe=universe,
        )
        sc = score_fn(row)
        saved = {**params, "scan_every_bars": scan_bars}
        trial = {
            "params": saved,
            "score": round(sc, 4),
            **{k: row.get(k) for k in ("return_pct", "profit_factor", "max_drawdown_pct", "trades", "win_rate_pct")},
        }
        trials.append(trial)
        if sc > best_score:
            best_score = sc
            best_params = saved
            best_row = row

    trials.sort(key=lambda t: t["score"], reverse=True)
    return {
        "best_params": best_params,
        "best_score": round(best_score, 4),
        "best_result": best_row,
        "trials": trials,
        "universe_symbols": len(universe.symbols) if universe else 0,
    }
