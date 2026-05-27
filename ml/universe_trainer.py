"""
Continuous ML training across every live USDT perpetual on the exchange.

Rotates through the full universe in the background, accumulates samples,
and refits the ensemble when a full cycle completes (and on demand).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from ml.features import FEATURE_NAMES, build_training_matrix
from ml.matrix_kwargs import training_matrix_kwargs
from ml.outcomes import TradeOutcomeTracker
from ml.trainer import SignalModel
from universe import load_training_markets, training_symbol_cap

log = logging.getLogger(__name__)


class ContinuousMlTrainer:
    def __init__(
        self,
        ex,
        settings,
        *,
        on_model_updated: Callable[[], None] | None = None,
    ) -> None:
        self.ex = ex
        self.settings = settings
        self.on_model_updated = on_model_updated
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._force_fit = threading.Event()
        self._last_refit_request_ts = 0.0
        self._last_refit_ts = 0.0
        self._shards_since_refit = 0
        self._bootstrapped = False
        self._last_outcome_labels = 0
        self._last_refit_skip_log = 0.0
        self._shard_dir = settings.state_dir / "ml_shards"
        self._state_path = settings.state_dir / "ml_trainer_state.json"
        self._markets: list = []
        self._idx = 0
        self._load_state()

    def _load_state(self) -> None:
        if self._state_path.exists():
            try:
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._idx = int(raw.get("idx", 0))
                self._bootstrapped = bool(raw.get("bootstrapped", False))
                self._last_refit_ts = float(raw.get("last_refit_ts", 0))
            except Exception:
                self._idx = 0
        if self.shard_count() >= max(3, self.settings.ml_bootstrap_symbols // 2):
            self._bootstrapped = True

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(
                {
                    "idx": self._idx,
                    "universe_n": len(self._markets),
                    "bootstrapped": self._bootstrapped,
                    "last_refit_ts": self._last_refit_ts,
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def refresh_universe(self) -> int:
        cap = training_symbol_cap(self.settings)
        self._markets = load_training_markets(self.ex, cap=cap)
        if self._idx >= len(self._markets):
            self._idx = 0
        return len(self._markets)

    def _maybe_rotate_stale_shards(self) -> None:
        """Drop 30-dim (or other) legacy shards before training starts."""
        if not self._shard_dir.exists():
            return
        feat_dim = len(FEATURE_NAMES)
        for path in self._shard_dir.glob("*.npz"):
            try:
                with np.load(path) as data:
                    if int(data["X"].shape[1]) != feat_dim:
                        self._rotate_shard_store(reason="startup_dim_mismatch")
                        return
            except Exception:
                continue

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        n = self.refresh_universe()
        self._maybe_rotate_stale_shards()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ml-universe-trainer", daemon=True)
        self._thread.start()
        log.info(
            "ML universe trainer started — %d assets | train-as-you-go | bootstrap=%d | refit every %d shards or %dm",
            n,
            self.settings.ml_bootstrap_symbols,
            self.settings.ml_refit_min_shards,
            self.settings.ml_refit_interval_minutes,
        )

    def shard_count(self) -> int:
        if not self._shard_dir.exists():
            return 0
        return len(list(self._shard_dir.glob("*.npz")))

    def maybe_refit_from_outcomes(self, tracker: TradeOutcomeTracker) -> None:
        """Refit when enough new live trade labels accumulated."""
        _, y = tracker.load_labelled_samples()
        n = len(y)
        if n - self._last_outcome_labels >= self.settings.ml_outcome_refit_min_new:
            self._last_outcome_labels = n
            log.info("ML outcome feedback: %d labelled fills — scheduling refit", n)
            self.request_full_refit()

    def stop(self) -> None:
        self._stop.set()

    def request_full_refit(self) -> None:
        now = time.time()
        if self._force_fit.is_set() and now - self._last_refit_request_ts < 120:
            return
        self._last_refit_request_ts = now
        self._force_fit.set()

    def _loop(self) -> None:
        last_refresh = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_refresh > 3600:
                    n = self.refresh_universe()
                    last_refresh = time.time()
                    if n == 0:
                        self._stop.wait(60)
                        continue

                if not self._bootstrapped and self.settings.ml_continuous_train:
                    self._bootstrap()
                    self._bootstrapped = True
                    self._save_state()

                if self._force_fit.is_set():
                    self._force_fit.clear()
                    self._aggregate_and_train(reason="manual")
                elif self._markets:
                    if self._train_one(self._markets[self._idx]):
                        self._shards_since_refit += 1
                    self._idx = (self._idx + 1) % len(self._markets)
                    self._save_state()

                    interval_sec = self.settings.ml_refit_interval_minutes * 60
                    n_shards = self.shard_count()
                    timer_due = (
                        self._last_refit_ts > 0
                        and time.time() - self._last_refit_ts >= interval_sec
                        and n_shards >= max(3, self.settings.ml_refit_min_shards // 2)
                    )
                    batch_due = (
                        self._shards_since_refit >= self.settings.ml_refit_min_shards
                        and n_shards >= 3
                    )

                    if self._idx == 0:
                        log.info(
                            "ML universe cycle complete (%d assets) — refitting ensemble",
                            len(self._markets),
                        )
                        self._aggregate_and_train(reason="cycle")
                    elif batch_due:
                        self._aggregate_and_train(reason="shard_batch")
                    elif timer_due:
                        self._aggregate_and_train(reason="timer")
            except Exception:
                log.exception("ML universe trainer tick failed")
            self._stop.wait(2.5)

    def _bootstrap(self) -> None:
        n = min(self.settings.ml_bootstrap_symbols, len(self._markets))
        if n <= 0:
            return
        log.info("ML bootstrap: seeding %d/%d universe symbols for first deploy...", n, len(self._markets))
        for i in range(n):
            if self._stop.is_set():
                return
            self._train_one(self._markets[i])
        self._idx = n % len(self._markets)
        if self.shard_count() >= 3:
            self._aggregate_and_train(reason="bootstrap")
        else:
            log.warning("ML bootstrap: insufficient shards (%d) — continuing scan", self.shard_count())

    def _train_one(self, market) -> bool:
        symbol = market.symbol
        try:
            ohlcv_1m = self.ex.fetch_ohlcv(symbol, "1m", self.settings.ml_history_bars)
            ohlcv_5m = self.ex.fetch_ohlcv(
                symbol, "5m", min(500, self.settings.ml_history_bars // 5)
            )
            funding = self.ex.fetch_funding_rate(symbol)
            batch = build_training_matrix(
                ohlcv_1m,
                ohlcv_5m,
                funding_rate=funding,
                min_samples=8,
                **training_matrix_kwargs(self.settings),
            )
            if batch is None:
                log.debug("ml shard skip %s (insufficient labels)", symbol)
                return False
            X, y = batch
            safe = symbol.replace("/", "_").replace(":", "_")
            path = self._shard_dir / f"{safe}.npz"
            self._shard_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, X=X, y=y)
            log.info("ml shard saved %s samples=%d dim=%d", symbol, len(y), X.shape[1])
            return True
        except Exception:
            log.exception("ml shard failed %s", symbol)
            return False

    def _rotate_shard_store(self, *, reason: str) -> None:
        """Archive stale shards (Windows-safe — avoids np.load file locks on unlink)."""
        if not self._shard_dir.exists():
            self._shard_dir.mkdir(parents=True, exist_ok=True)
            return
        stale = self.settings.state_dir / f"ml_shards_stale_{int(time.time())}"
        try:
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)
            self._shard_dir.rename(stale)
        except OSError as e:
            log.warning("ML shard archive failed (%s): %s — wiping in place", reason, e)
            for path in self._shard_dir.glob("*.npz"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._shard_dir.mkdir(parents=True, exist_ok=True)
        self._shards_since_refit = 0
        log.warning(
            "ML shards reset (%s) — retraining on %d-feature vectors",
            reason,
            len(FEATURE_NAMES),
        )

    def _aggregate_and_train(self, *, reason: str) -> None:
        with self._lock:
            shards = sorted(self._shard_dir.glob("*.npz")) if self._shard_dir.exists() else []
            if not shards:
                now = time.time()
                if now - self._last_refit_skip_log > 60.0:
                    self._last_refit_skip_log = now
                    log.warning("ML refit skipped (%s): no shards yet — trainer still seeding", reason)
                return
            xs, ys = [], []
            for path in shards:
                try:
                    with np.load(path) as data:
                        xs.append(np.asarray(data["X"]))
                        ys.append(np.asarray(data["y"]))
                except Exception:
                    continue
            if not xs:
                return
            feat_dim = len(FEATURE_NAMES)
            if any(x.shape[1] != feat_dim for x in xs):
                self._rotate_shard_store(reason=reason)
                return
            X = np.vstack(xs)
            y = np.concatenate(ys)
            tracker = TradeOutcomeTracker(
                self.settings.state_dir, self.settings.ml_real_feedback_max_samples
            )
            X_fb, y_fb = tracker.load_labelled_samples()
            model = SignalModel()
            metrics = model.fit(
                X,
                y,
                symbols=len(self._markets),
                walk_forward_splits=self.settings.ml_walk_forward_splits,
                min_train_samples=self.settings.ml_walk_forward_min_train,
                X_feedback=X_fb if len(y_fb) > 0 else None,
                y_feedback=y_fb if len(y_fb) > 0 else None,
                min_deploy_samples=self.settings.ml_min_deploy_samples,
                purge_gap=self.settings.ml_purge_gap,
                embargo_pct=self.settings.ml_embargo_pct,
            )
            model.save(
                self.settings.state_dir / "signal_model.joblib",
                self.settings.state_dir / "signal_model_meta.json",
            )
            self._last_refit_ts = time.time()
            self._shards_since_refit = 0
            self._save_state()
            log.info(
                "ML refit (%s) universe=%d shards=%d samples=%d val_acc=%.1f%% deployed=%s",
                reason,
                len(self._markets),
                len(shards),
                len(y),
                metrics.val_accuracy * 100,
                metrics.deployed,
            )
            if self.on_model_updated:
                self.on_model_updated()
