"""Forward ML refit on train-window candles (walk-forward, no lookahead)."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


def refit_forward_model(
    *,
    assets: list[dict[str, Any]],
    train_start_ms: int,
    train_end_ms: int,
    settings: Any,
    max_symbols: int = 40,
) -> dict[str, Any]:
    """Build shards from train window only, refit ensemble, return OOS-safe metrics."""
    from config import load_settings
    from god_backtest.candle_cache import load_symbol_candles
    from ml.features import build_training_matrix
    from ml.matrix_kwargs import training_matrix_kwargs
    from ml.trainer import SignalModel

    settings = settings or load_settings()
    X_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    used = 0

    for asset in assets[:max_symbols]:
        iid = asset["inst_id"]
        try:
            c5, _c1 = load_symbol_candles(iid, start_ms=train_start_ms, end_ms=train_end_ms)
            if len(c5) < 120:
                continue
            # Use 5m as both fast/slow for shard build during backtest (train window only).
            batch = build_training_matrix(
                c5,
                c5,
                funding_rate=0.0,
                min_samples=8,
                **training_matrix_kwargs(settings),
            )
            if batch is None:
                continue
            X, y = batch
            X_parts.append(X)
            y_parts.append(y)
            used += 1
        except Exception as exc:
            log.debug("ml refit skip %s: %s", iid, exc)

    if not X_parts:
        return {"ok": False, "reason": "no_shards", "symbols": 0}

    X_all = np.vstack(X_parts)
    y_all = np.concatenate(y_parts)
    if len(y_all) < 80:
        return {"ok": False, "reason": "insufficient_samples", "samples": int(len(y_all))}

    model = SignalModel()
    metrics = model.fit(
        X_all,
        y_all,
        symbols=used,
        walk_forward_splits=3,
        min_train_samples=min(80, len(y_all) // 3),
        min_deploy_samples=min(60, len(y_all) // 2),
    )
    return {
        "ok": bool(metrics.deployed),
        "symbols": used,
        "samples": int(len(y_all)),
        "val_accuracy": round(metrics.val_accuracy * 100, 2),
        "deployed": metrics.deployed,
    }
