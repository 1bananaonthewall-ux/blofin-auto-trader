"""Regime-aware labels — K-means on forward moves (Nature Sci Reports 2026)."""

from __future__ import annotations

import numpy as np


def cluster_forward_moves(moves: np.ndarray, *, k: int = 3) -> np.ndarray:
    """Simple 1-D K-means on signed forward returns. Returns cluster index per sample."""
    x = np.asarray(moves, dtype=np.float64).reshape(-1, 1)
    if len(x) < k * 5:
        return np.zeros(len(x), dtype=np.int64)
    centers = np.percentile(x[:, 0], np.linspace(10, 90, k))
    labels = np.zeros(len(x), dtype=np.int64)
    for _ in range(12):
        dist = np.abs(x - centers.reshape(1, -1))
        labels = np.argmin(dist, axis=1)
        for j in range(k):
            mask = labels == j
            if mask.any():
                centers[j] = float(np.mean(x[mask, 0]))
    return labels


def harsh_move_cluster(moves: np.ndarray, *, k: int = 3) -> int:
    """Cluster id with largest absolute mean move — 'harsh' regime."""
    labels = cluster_forward_moves(moves, k=k)
    best_c, best_mag = 0, -1.0
    for c in range(k):
        mask = labels == c
        if not mask.any():
            continue
        mag = abs(float(np.mean(moves[mask])))
        if mag > best_mag:
            best_mag = mag
            best_c = c
    return best_c
