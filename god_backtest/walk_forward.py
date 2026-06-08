"""Walk-forward parameter search optimized for out-of-sample performance."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from bobs_bots.specs import BotSpec
from god_backtest.simulator import backtest_symbol_window, score_aggregate, spec_from_params

log = logging.getLogger(__name__)

# Compact grid — tuned for forward OOS score, not in-sample curve-fit.
PARAM_GRID: list[dict[str, Any]] = [
    {"min_confluence": 0.44, "min_composite_score": 45.0, "min_confidence": 0.53, "entry_gap_bars": 3, "min_agreeing": 3},
    {"min_confluence": 0.45, "min_composite_score": 46.0, "min_confidence": 0.54, "entry_gap_bars": 4},
    {"min_confluence": 0.46, "min_composite_score": 47.0, "min_confidence": 0.55, "entry_gap_bars": 4},
    {"min_confluence": 0.47, "min_composite_score": 48.0, "min_confidence": 0.56, "entry_gap_bars": 5},
    {"min_confluence": 0.48, "min_composite_score": 49.0, "min_confidence": 0.57, "entry_gap_bars": 6},
    {"min_confluence": 0.49, "min_composite_score": 50.0, "min_confidence": 0.58, "entry_gap_bars": 7},
    {"min_confluence": 0.50, "min_composite_score": 51.0, "min_confidence": 0.59, "entry_gap_bars": 8},
]


def _run_window(
    spec: BotSpec,
    assets: list[dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    starting_pot: float,
    max_workers: int,
    ws_tails: dict[str, dict[str, list[list[float]]]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def one(asset: dict[str, Any]) -> dict[str, Any] | None:
        iid = asset["inst_id"]
        tail = (ws_tails or {}).get(iid)
        row = backtest_symbol_window(
            spec,
            inst_id=iid,
            starting_pot=starting_pot,
            start_ms=start_ms,
            end_ms=end_ms,
            ws_tail=tail,
        )
        return None if row.get("error") else row

    workers = max(1, min(max_workers, len(assets) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, a): a for a in assets}
        for fut in as_completed(futs):
            try:
                row = fut.result()
                if row:
                    rows.append(row)
            except Exception as exc:
                log.debug("window backtest failed: %s", exc)
    return rows


def optimize_fold(
    assets: list[dict[str, Any]],
    *,
    train_start_ms: int,
    train_end_ms: int,
    starting_pot: float,
    max_workers: int,
    tune_symbols: int = 60,
) -> tuple[dict[str, Any], BotSpec, float]:
    """Pick params on train window (subset of symbols for speed)."""
    tune_set = assets[:tune_symbols]
    best_params = PARAM_GRID[1]
    best_score = -1e18
    best_spec = spec_from_params(**best_params)

    for params in PARAM_GRID:
        spec = spec_from_params(**params)
        rows = _run_window(
            spec,
            tune_set,
            start_ms=train_start_ms,
            end_ms=train_end_ms,
            starting_pot=starting_pot,
            max_workers=max_workers,
        )
        sc = score_aggregate(rows)
        if sc > best_score:
            best_score = sc
            best_params = params
            best_spec = spec

    return best_params, best_spec, best_score


def run_walk_forward_fold(
    fold: dict[str, int],
    assets: list[dict[str, Any]],
    *,
    starting_pot: float,
    max_workers: int,
    ws_tails: dict[str, dict[str, list[list[float]]]] | None = None,
) -> dict[str, Any]:
    best_params, best_spec, train_score = optimize_fold(
        assets,
        train_start_ms=fold["train_start_ms"],
        train_end_ms=fold["train_end_ms"],
        starting_pot=starting_pot,
        max_workers=max_workers,
    )
    oos_rows = _run_window(
        best_spec,
        assets,
        start_ms=fold["test_start_ms"],
        end_ms=fold["test_end_ms"],
        starting_pot=starting_pot,
        max_workers=max_workers,
        ws_tails=ws_tails,
    )
    oos_score = score_aggregate(oos_rows)
    return {
        "fold": fold,
        "best_params": best_params,
        "train_score": round(train_score, 4),
        "oos_score": round(oos_score, 4),
        "oos_symbols": len(oos_rows),
        "oos_avg_return_pct": round(
            sum(r.get("return_pct", 0) for r in oos_rows) / len(oos_rows) if oos_rows else 0.0,
            2,
        ),
        "oos_avg_profit_factor": round(
            sum(r.get("profit_factor", 0) for r in oos_rows) / len(oos_rows) if oos_rows else 0.0,
            2,
        ),
        "oos_total_trades": sum(r.get("trades", 0) for r in oos_rows),
        "top_oos": sorted(oos_rows, key=lambda r: r.get("return_pct", 0), reverse=True)[:15],
        "spec": {
            "id": best_spec.id,
            "min_confluence": best_spec.min_confluence,
            "min_composite_score": best_spec.min_composite_score,
            "min_confidence": best_spec.min_confidence,
            "entry_gap_bars": best_spec.entry_gap_bars,
        },
    }
