"""Platt-style probability calibration for ML outputs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def load_calibrator(state_dir: Path) -> dict | None:
    path = state_dir / "ml_calibration.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_calibrator(state_dir: Path, long_a: float, long_b: float, short_a: float, short_b: float) -> None:
    path = state_dir / "ml_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "long": {"a": long_a, "b": long_b},
                "short": {"a": short_a, "b": short_b},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def fit_platt(raw_probs: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """Simple Platt scaling via logit regression (2-param)."""
    eps = 1e-6
    p = np.clip(raw_probs, eps, 1 - eps)
    logit = np.log(p / (1 - p))
    X = np.column_stack([logit, np.ones(len(logit))])
    try:
        coef, _, _, _ = np.linalg.lstsq(X, labels.astype(float), rcond=None)
        return float(coef[0]), float(coef[1])
    except Exception:
        return 1.0, 0.0


def calibrate_side(p_raw: float, side: str, cal: dict | None) -> float:
    if not cal:
        return p_raw
    row = cal.get("long" if side == "long" else "short") or {}
    a = float(row.get("a", 1.0))
    b = float(row.get("b", 0.0))
    eps = 1e-6
    p = max(eps, min(1 - eps, p_raw))
    logit = np.log(p / (1 - p))
    return float(_sigmoid(a * logit + b))


def calibrate_pair(p_long: float, p_short: float, state_dir: Path) -> tuple[float, float]:
    cal = load_calibrator(state_dir)
    if not cal:
        return p_long, p_short
    cl = calibrate_side(p_long, "long", cal)
    cs = calibrate_side(p_short, "short", cal)
    total = cl + cs
    if total <= 0:
        return p_long, p_short
    return cl / total, cs / total
