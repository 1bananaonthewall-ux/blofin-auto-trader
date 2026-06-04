#!/usr/bin/env python3
"""Download history and train the adaptive signal model with walk‑forward
validation and real‑fill outcome feedback."""

from __future__ import annotations

import logging
import sys
import time

import numpy as np

from config import load_settings
from exchange_client import BlofinExchange
from markets import inst_id_to_symbol
from ml.features import build_training_matrix
from ml.matrix_kwargs import training_matrix_kwargs
from ml.outcomes import TradeOutcomeTracker
from ml.trainer import SignalModel
from universe import load_training_markets, training_symbol_cap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_model")


def main() -> int:
    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    cap = training_symbol_cap(settings)
    markets = load_training_markets(ex, cap=cap)

    cap_label = "all exchange" if cap <= 0 else str(cap)
    log.info("training on %d symbols (cap %s)", len(markets), cap_label)

    all_x: list[np.ndarray] = []
    all_y: list[np.ndarray] = []

    for i, market in enumerate(markets):
        symbol = market.symbol
        try:
            ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", settings.ml_history_bars)
            ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", min(500, settings.ml_history_bars // 5))
            funding = ex.fetch_funding_rate(symbol)
            batch = build_training_matrix(
                ohlcv_1m,
                ohlcv_5m,
                funding_rate=funding,
                **training_matrix_kwargs(settings),
            )
            if batch is None:
                log.info("[%d/%d] skip %s (insufficient samples)", i + 1, len(markets), symbol)
                continue
            X, y = batch
            all_x.append(X)
            all_y.append(y)
            log.info("[%d/%d] %s samples=%d", i + 1, len(markets), symbol, len(y))
            time.sleep(0.15)
        except Exception:
            log.exception("failed %s", symbol)

    if not all_x:
        log.error("no training data collected")
        return 1

    X = np.vstack(all_x)
    y = np.concatenate(all_y)
    log.info(
        "total samples=%d (long=%d short=%d)",
        len(y),
        int((y == 0).sum()),
        int((y == 1).sum()),
    )

    # --- Load real‑fill outcome feedback ---
    tracker = TradeOutcomeTracker(settings.state_dir, settings.ml_real_feedback_max_samples)
    X_fb, y_fb = tracker.load_labelled_samples(margin_mode=settings.margin_mode)
    if len(y_fb) > 0:
        log.info("loaded %d real‑feedback samples for training", len(y_fb))

    model = SignalModel()
    metrics = model.fit(
        X,
        y,
        symbols=len(markets),
        walk_forward_splits=settings.ml_walk_forward_splits,
        min_train_samples=settings.ml_walk_forward_min_train,
        X_feedback=X_fb if len(y_fb) > 0 else None,
        y_feedback=y_fb if len(y_fb) > 0 else None,
        min_deploy_samples=settings.ml_min_deploy_samples,
        purge_gap=settings.ml_purge_gap,
        embargo_pct=settings.ml_embargo_pct,
    )
    model.save(
        settings.state_dir / "signal_model.joblib",
        settings.state_dir / "signal_model_meta.json",
    )

    log.info(
        "train_acc=%.2f%% val_acc=%.2f%% long_p=%.2f%% short_p=%.2f%% "
        "wf_splits=%d feedback=%d deployed=%s",
        metrics.train_accuracy * 100,
        metrics.val_accuracy * 100,
        metrics.val_long_precision * 100,
        metrics.val_short_precision * 100,
        metrics.walk_forward_splits,
        metrics.feedback_samples,
        metrics.deployed,
    )
    if not metrics.deployed:
        log.warning("model did not pass quality gates — bot will fall back to rule strategy")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())