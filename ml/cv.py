"""Purged time-series CV — reduces label leakage (López de Prado / AFML)."""

from __future__ import annotations

import numpy as np
from sklearn.model_selection import TimeSeriesSplit


class PurgedTimeSeriesSplit:
    """Walk-forward splits with purge gap + post-test embargo."""

    def __init__(
        self,
        n_splits: int = 5,
        *,
        purge_gap: int = 5,
        embargo_pct: float = 0.01,
    ) -> None:
        self.n_splits = max(2, n_splits)
        self.purge_gap = max(1, purge_gap)
        self.embargo_pct = max(0.0, min(0.05, embargo_pct))

    def split(self, X: np.ndarray, y: np.ndarray | None = None):
        n = len(X)
        embargo = max(1, int(n * self.embargo_pct))
        tscv = TimeSeriesSplit(n_splits=self.n_splits)
        for train_idx, test_idx in tscv.split(X):
            if len(train_idx) == 0 or len(test_idx) == 0:
                continue
            test_start = int(test_idx[0])
            test_end = int(test_idx[-1])
            purge_before = max(0, test_start - self.purge_gap)
            embargo_after = min(n - 1, test_end + embargo)
            keep = train_idx[
                (train_idx < purge_before) | (train_idx > embargo_after)
            ]
            if len(keep) < 50 or len(test_idx) < 10:
                continue
            yield keep, test_idx
