"""
ML health guard — verify continuous training + forward feedback, repair when stale.

Runs hourly (full audit/repair) and every 5m via log_watch (flags only).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

STATE_NAME = "ml_health.json"
ACTIONS_NAME = "ml_health_actions.jsonl"
MIN_HOURLY_INTERVAL = 3000.0
MIN_LIGHT_INTERVAL = 900.0

LOG_SHARD_RE = re.compile(r"ml shard saved")
LOG_REFIT_RE = re.compile(r"ML refit \(|ML forward refit \(|merging \d+ real-feedback")
LOG_TRAINER_RE = re.compile(r"ML universe trainer started")


@dataclass
class MlHealthReport:
    ok: bool = True
    ts: float = 0.0
    signal_mode: str = ""
    continuous_train: bool = False
    shards: int = 0
    shard_min: int = 3
    labels_total: int = 0
    labels_cursor: int = 0
    labels_pending: int = 0
    feedback_in_model: int = 0
    model_deployed: bool = False
    model_age_hours: float = 0.0
    refit_age_hours: float = 0.0
    feature_match: bool = True
    log_shard_recent: bool = False
    log_refit_recent: bool = False
    log_trainer_recent: bool = False
    issues: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_NAME


def _guard_state(state_dir: Path) -> dict[str, Any]:
    path = _state_path(state_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_report(state_dir: Path, report: MlHealthReport) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _state_path(state_dir).write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def _append_action(state_dir: Path, row: dict[str, Any]) -> None:
    path = state_dir / ACTIONS_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _tail_log(root: Path, n: int = 2500) -> list[str]:
    path = root / "logs" / "bot.log"
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _recent_shard_activity(shard_dir: Path, within_sec: float) -> bool:
    if not shard_dir.is_dir():
        return False
    cutoff = time.time() - within_sec
    for path in shard_dir.glob("*.npz"):
        try:
            if path.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def _bot_running(root: Path) -> bool:
    try:
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, str(root / "scripts" / "stack_status.py")],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(root),
        )
        return "bot.py: RUNNING" in (r.stdout or "")
    except Exception:
        return False


def audit(
    settings: "Settings",
    *,
    root: Path | None = None,
) -> MlHealthReport:
    from ml.features import FEATURE_NAMES
    from ml.outcomes import TradeOutcomeTracker

    state_dir = settings.state_dir
    project_root = root or Path(__file__).resolve().parent
    now = time.time()
    rep = MlHealthReport(
        ts=now,
        signal_mode=settings.signal_mode,
        continuous_train=bool(getattr(settings, "ml_continuous_train", True)),
        shard_min=max(3, getattr(settings, "ml_bootstrap_symbols", 25) // 4),
    )

    shard_dir = state_dir / "ml_shards"
    rep.shards = len(list(shard_dir.glob("*.npz"))) if shard_dir.exists() else 0

    trainer_state: dict[str, Any] = {}
    ts_path = state_dir / "ml_trainer_state.json"
    if ts_path.is_file():
        try:
            trainer_state = json.loads(ts_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    rep.labels_cursor = int(trainer_state.get("last_outcome_labels", 0))
    last_refit_ts = float(trainer_state.get("last_refit_ts", 0))
    if last_refit_ts > 0:
        rep.refit_age_hours = round((now - last_refit_ts) / 3600.0, 2)

    tracker = TradeOutcomeTracker(state_dir, settings.ml_real_feedback_max_samples)
    _, y = tracker.load_labelled_samples(margin_mode=settings.margin_mode)
    rep.labels_total = len(y)
    rep.labels_pending = max(0, rep.labels_total - rep.labels_cursor)

    meta_path = state_dir / "signal_model_meta.json"
    model_path = state_dir / "signal_model.joblib"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            rep.model_deployed = bool(meta.get("deployed"))
            rep.feedback_in_model = int(meta.get("feedback_samples", 0))
            trained_at = meta.get("trained_at", "")
            if trained_at:
                from datetime import datetime

                try:
                    dt = datetime.fromisoformat(str(trained_at).replace("Z", "+00:00"))
                    rep.model_age_hours = round(
                        (now - dt.timestamp()) / 3600.0, 2
                    )
                except Exception:
                    pass
            feat_names = meta.get("feature_names") or []
            rep.feature_match = len(feat_names) == len(FEATURE_NAMES)
        except Exception:
            rep.issues.append("model_meta_unreadable")
    else:
        rep.issues.append("no_model_meta")

    if not model_path.is_file():
        rep.issues.append("no_model_file")

    lines = _tail_log(project_root)
    hour_ago = now - 3600
    for line in lines[-800:]:
        if "2026-" not in line[:20] and "2025-" not in line[:20]:
            continue
        # Recent log activity in tail is enough for guard purposes.
        if LOG_SHARD_RE.search(line):
            rep.log_shard_recent = True
        if LOG_REFIT_RE.search(line):
            rep.log_refit_recent = True
        if LOG_TRAINER_RE.search(line):
            rep.log_trainer_recent = True

    if not rep.log_shard_recent:
        rep.log_shard_recent = _recent_shard_activity(shard_dir, 7200.0)

    if settings.signal_mode != "ml":
        rep.issues.append("signal_mode_not_ml")
    if not rep.continuous_train:
        rep.issues.append("ml_continuous_train_off")
    if rep.shards < rep.shard_min:
        rep.issues.append(f"low_shards={rep.shards}")
    if not rep.model_deployed:
        rep.issues.append("model_not_deployed")
    if not rep.feature_match:
        rep.issues.append("feature_mismatch")
    if rep.refit_age_hours > max(2.0, settings.ml_refit_interval_minutes / 60.0 * 1.5):
        rep.issues.append(f"stale_refit={rep.refit_age_hours}h")
    if rep.labels_pending >= int(settings.ml_outcome_refit_min_new):
        rep.issues.append(f"pending_labels={rep.labels_pending}")
    if rep.feedback_in_model < rep.labels_total - 5 and rep.labels_total >= 30:
        rep.issues.append(
            f"feedback_lag model={rep.feedback_in_model} disk={rep.labels_total}"
        )
    if not rep.log_shard_recent and not rep.log_refit_recent:
        rep.issues.append("no_recent_ml_activity")

    rep.ok = len(rep.issues) == 0
    return rep


def _set_flag(state_dir: Path, name: str, payload: dict[str, Any]) -> None:
    path = state_dir / name
    path.write_text(json.dumps({**payload, "ts": time.time()}, indent=2), encoding="utf-8")


def repair(
    settings: "Settings",
    report: MlHealthReport,
    ex=None,
    *,
    root: Path | None = None,
    force: bool = False,
    hourly: bool = False,
) -> MlHealthReport:
    """Apply fixes: flags for live bot, offline seed/refit when safe."""
    state_dir = settings.state_dir
    project_root = root or Path(__file__).resolve().parent
    guard = _guard_state(state_dir)
    last = float(guard.get("last_repair_ts", 0))
    min_iv = MIN_HOURLY_INTERVAL if hourly else MIN_LIGHT_INTERVAL
    if not force and time.time() - last < min_iv:
        report.actions.append("repair_skipped_cooldown")
        return report

    if settings.signal_mode != "ml":
        report.actions.append("skip_not_ml_mode")
        _save_report(state_dir, report)
        return report

    bot_live = _bot_running(project_root)
    issues = set(report.issues)

    if "feature_mismatch" in issues or "no_model_meta" in issues or "no_model_file" in issues:
        _set_flag(state_dir, "ml_force_refit.flag", {"reason": "health_guard_schema"})
        report.actions.append("flag_force_refit")

    if any(i.startswith("low_shards") for i in issues):
        if ex is not None and (hourly or force) and not bot_live:
            try:
                from ml.universe_trainer import seed_shards_offline

                n = max(8, settings.ml_bootstrap_symbols // 2)
                saved = seed_shards_offline(ex, settings, n=n)
                report.actions.append(f"seed_shards={saved}")
                report.shards = report.shards + saved
            except Exception as exc:
                report.actions.append(f"seed_err:{exc}")
                _set_flag(state_dir, "ml_bootstrap_due.flag", {"reason": "low_shards"})
        else:
            _set_flag(state_dir, "ml_bootstrap_due.flag", {"reason": "low_shards"})
            report.actions.append("flag_bootstrap")

    pending = report.labels_pending >= int(settings.ml_outcome_refit_min_new)
    stale_refit = any(i.startswith("stale_refit") for i in issues)
    feedback_lag = any(i.startswith("feedback_lag") for i in issues)
    not_deployed = "model_not_deployed" in issues

    if pending or stale_refit or feedback_lag or not_deployed:
        _set_flag(state_dir, "ml_force_refit.flag", {"reason": "health_guard_stale"})
        report.actions.append("flag_force_refit")

    # Hourly: run offline refit when shards exist and model stale / labels pending.
    can_offline_refit = (
        ex is not None
        and report.shards >= 3
        and (hourly or force)
        and (stale_refit or feedback_lag or not_deployed or pending)
    )
    # Offline refit only when bot is stopped — live bot uses in-process trainer via flags.
    if can_offline_refit and not bot_live:
        try:
            from ml.universe_trainer import run_offline_refit

            ref = run_offline_refit(settings, ex, reason="hourly_guard")
            if ref.get("ok"):
                report.actions.append(
                    f"offline_refit samples={ref.get('samples')} "
                    f"feedback={ref.get('feedback')} "
                    f"val={float(ref.get('val_accuracy', 0))*100:.1f}%"
                )
                _set_flag(state_dir, "ml_reload.flag", {"reason": "hourly_offline_refit"})
                report.model_deployed = bool(ref.get("deployed"))
                report.refit_age_hours = 0.0
            else:
                report.actions.append(f"offline_refit_skip:{ref.get('error', '?')}")
        except Exception as exc:
            report.actions.append(f"offline_refit_err:{exc}")

    # Sync forward cursor after successful refit path.
    if report.actions and ex is not None:
        try:
            from ml.outcomes import TradeOutcomeTracker
            from ml.universe_trainer import ContinuousMlTrainer

            tr = ContinuousMlTrainer(ex, settings)
            tr.sync_outcome_label_cursor(TradeOutcomeTracker(state_dir, settings.ml_real_feedback_max_samples))
        except Exception:
            pass

    guard["last_repair_ts"] = time.time()
    guard["last_report"] = asdict(report)
    _state_path(state_dir).write_text(json.dumps(guard, indent=2), encoding="utf-8")

    if report.actions:
        _append_action(state_dir, asdict(report))
        log.warning(
            "ML HEALTH | shards=%d labels=%d pending=%d deployed=%s | %s",
            report.shards,
            report.labels_total,
            report.labels_pending,
            report.model_deployed,
            " | ".join(report.actions),
        )
    elif not report.ok:
        log.info(
            "ML HEALTH watch | shards=%d pending=%d issues=%s",
            report.shards,
            report.labels_pending,
            ",".join(report.issues[:4]),
        )

    report.ok = len(report.issues) == 0 and report.model_deployed
    _save_report(state_dir, report)
    return report


def tick(
    settings: "Settings",
    ex=None,
    *,
    root: Path | None = None,
    force: bool = False,
    hourly: bool = False,
) -> dict[str, Any]:
    report = audit(settings, root=root)
    report = repair(settings, report, ex=ex, root=root, force=force, hourly=hourly)
    return asdict(report)


def run_standalone() -> int:
    from config import load_settings
    from exchange_client import BlofinExchange

    settings = load_settings()
    ex = None
    if settings.signal_mode == "ml":
        ex = BlofinExchange(settings)
        ex.load()
    rep = tick(settings, ex=ex, force=True, hourly=True)
    print(json.dumps(rep, indent=2))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(run_standalone())
