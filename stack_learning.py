"""
Automatic ML refit + cortex knowledge rebuild — no manual train_model / train_local_cortex.

Runs in background threads inside bot.py after dashboard Restart Bot.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ml.features import FEATURE_NAMES

if TYPE_CHECKING:
    from config import Settings
    from ml.outcomes import TradeOutcomeTracker
    from ml.predictor import MLPredictor
    from ml.universe_trainer import ContinuousMlTrainer

log = logging.getLogger(__name__)

_last_cortex_train_ts = 0.0
_cortex_lock = threading.Lock()


def ml_feature_mismatch(ml: "MLPredictor") -> bool:
    if ml.model is None or ml.model.metrics is None:
        return True
    names = ml.model.metrics.feature_names or []
    return len(names) != len(FEATURE_NAMES)


def ml_needs_startup_refit(ml: "MLPredictor") -> bool:
    if ml_feature_mismatch(ml):
        return True
    if not ml.is_ready():
        return True
    return False


def schedule_ml_startup_refit(
    ml_trainer: "ContinuousMlTrainer | None",
    ml: "MLPredictor",
    *,
    reason: str = "startup",
    force: bool = False,
) -> None:
    if not ml_trainer:
        return
    if not force and not ml_needs_startup_refit(ml):
        return
    if ml_feature_mismatch(ml):
        log.warning(
            "ML schema drift — model has %s features, code has %s; rotating shards + immediate refit",
            len(ml.model.metrics.feature_names) if ml.model and ml.model.metrics else 0,
            len(FEATURE_NAMES),
        )
        ml_trainer.rotate_stale_shards(reason="feature_mismatch")
    log.info("ML auto-refit queued (%s)", reason)
    ml_trainer.request_full_refit()


def _count_closes_since(state_dir: Path, since_ts: float) -> int:
    path = state_dir / "trade_outcomes.jsonl"
    if not path.is_file():
        return 0
    n = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") not in ("outcome", "close"):
                continue
            ts = float(row.get("close_ts") or row.get("ts") or 0)
            if ts > 1e12:
                ts /= 1000.0
            if ts >= since_ts:
                n += 1
    except Exception:
        return 0
    return n


def cortex_should_train(state_dir: Path, settings: "Settings", *, force: bool = False) -> bool:
    if not getattr(settings, "cortex_auto_train", True):
        return False
    if force or getattr(settings, "cortex_train_on_startup", True) and _last_cortex_train_ts <= 0:
        return True
    meta_path = state_dir / "cortex" / "meta.json"
    trained_at = 0.0
    if meta_path.is_file():
        try:
            trained_at = float(json.loads(meta_path.read_text(encoding="utf-8")).get("trained_at", 0))
        except Exception:
            trained_at = 0.0
    interval = float(getattr(settings, "cortex_train_interval_minutes", 15)) * 60.0
    if time.time() - trained_at < interval:
        min_new = int(getattr(settings, "cortex_train_min_new_closes", 1))
        if _count_closes_since(state_dir, trained_at) < min_new:
            return False
    return True


def run_cortex_train(state_dir: Path, settings: "Settings", *, force: bool = False) -> dict[str, Any] | None:
    global _last_cortex_train_ts
    if not cortex_should_train(state_dir, settings, force=force):
        return None
    with _cortex_lock:
        if not force and not cortex_should_train(state_dir, settings, force=False):
            return None
        from local_cortex import train

        summary = train(state_dir, force=True)
        _last_cortex_train_ts = time.time()
        log.info(
            "cortex auto-trained: %s closes, %s examples, win_rate=%.1f%%",
            summary.get("closes", summary.get("entries", 0)),
            summary.get("examples", 0),
            float(summary.get("win_rate_pct", 0)),
        )
        return summary


def _cortex_thread(state_dir: Path, settings: "Settings", *, force: bool) -> None:
    try:
        run_cortex_train(state_dir, settings, force=force)
    except Exception:
        log.exception("cortex auto-train failed")


def schedule_cortex_train(
    state_dir: Path,
    settings: "Settings",
    *,
    force: bool = False,
    reason: str = "",
) -> None:
    if not getattr(settings, "cortex_auto_train", True):
        return
    if not force and not cortex_should_train(state_dir, settings, force=False):
        return
    tag = f" ({reason})" if reason else ""
    log.info("cortex auto-train scheduled%s", tag)
    threading.Thread(
        target=_cortex_thread,
        args=(state_dir, settings),
        kwargs={"force": force},
        daemon=True,
        name="cortex-auto-train",
    ).start()


def run_startup_learning(
    settings: "Settings",
    ml: "MLPredictor",
    ml_trainer: "ContinuousMlTrainer | None",
    tracker: "TradeOutcomeTracker | None",
) -> None:
    """Called once after bot starts — ML refit + cortex rebuild without user scripts."""

    def _work() -> None:
        if settings.signal_mode == "ml" and getattr(settings, "ml_auto_refit_on_startup", True):
            schedule_ml_startup_refit(ml_trainer, ml, reason="startup", force=True)
            if ml_trainer and tracker and getattr(settings, "ml_continuous_train", True):
                ml_trainer.maybe_refit_from_outcomes(tracker)
        if getattr(settings, "cortex_auto_train", True) and getattr(
            settings, "cortex_train_on_startup", True
        ):
            schedule_cortex_train(settings.state_dir, settings, force=True, reason="startup")

    threading.Thread(target=_work, daemon=True, name="stack-startup-learning").start()


def maybe_periodic_learning(
    settings: "Settings",
    ml: "MLPredictor",
    ml_trainer: "ContinuousMlTrainer | None",
    tracker: "TradeOutcomeTracker | None",
) -> None:
    """Lightweight tick hook from main loop (cortex interval + ML drift safety)."""
    if ml_trainer and ml_feature_mismatch(ml):
        schedule_ml_startup_refit(ml_trainer, ml, reason="feature_drift")
    schedule_cortex_train(settings.state_dir, settings, force=False, reason="periodic")
