"""
Per-trade lesson engine — examine every close, learn positives/negatives, apply in real time.

Hooked from ml.outcomes.record_close. Updates tuning, cooldowns, symbol memory, ML refit flags.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

LESSONS_JSONL = "trade_lessons.jsonl"
ACTIVE_STATE = "trade_lessons_active.json"
MODEL_FILE = "trade_lesson_model.joblib"
META_FILE = "trade_lesson_meta.json"
INSTANT_ML_FLAG = "ml_instant_refit.flag"

# Lesson categories the small ML policy can predict
LESSON_TAGS = (
    "reward_runner",
    "punish_choppy",
    "punish_symbol",
    "tighten_after_bleed",
    "loosen_after_win_streak",
    "ml_refit",
    "funding_headwind",
    "stress_caution",
    "punish_chase",
    "punish_wide_spread",
    "punish_bad_session",
)


@dataclass
class TradeLesson:
    ts: float
    symbol: str
    side: str
    outcome: str
    roe_pct: float | None
    pnl_usd: float | None
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    takeaway: str = ""
    actions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    entry_meta: dict[str, Any] = field(default_factory=dict)


def _active_path(state_dir: Path) -> Path:
    return state_dir / ACTIVE_STATE


def _load_active(state_dir: Path) -> dict[str, Any]:
    path = _active_path(state_dir)
    if not path.is_file():
        return {
            "win_streak": 0,
            "loss_streak": 0,
            "symbol_blocks": {},
            "pattern_blocks": {},
            "tuning_until": 0.0,
            "tuning_nudge": {},
            "recent": [],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"win_streak": 0, "loss_streak": 0, "symbol_blocks": {}, "pattern_blocks": {}, "recent": []}


def _save_active(state_dir: Path, raw: dict[str, Any]) -> None:
    raw["updated_at"] = time.time()
    state_dir.mkdir(parents=True, exist_ok=True)
    _active_path(state_dir).write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _append_lesson(state_dir: Path, lesson: TradeLesson) -> None:
    path = state_dir / LESSONS_JSONL
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(lesson), default=str) + "\n")


def _feature_row(record: dict[str, Any]) -> list[float]:
    return [
        float(record.get("chop_index") or 0.5),
        float(record.get("path_efficiency") or 0.3),
        float(record.get("run_score") or 0.5),
        float(record.get("signal_score") or 50) / 100.0,
        float(record.get("pick_score") or 0.5),
        1.0 if record.get("run_label") == "runner" else 0.0,
        1.0 if record.get("run_label") == "choppy" else 0.0,
        1.0 if str(record.get("outcome")) == "win" else 0.0,
        float(record.get("roe_pct") or 0) / 100.0,
    ]


def _predict_tags_ml(state_dir: Path, record: dict[str, Any]) -> list[str]:
    path = state_dir / MODEL_FILE
    if not path.is_file():
        return []
    try:
        import joblib
        import numpy as np

        model = joblib.load(path)
        X = np.array([_feature_row(record)], dtype=np.float64)
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba(X)[0]
            classes = list(getattr(model, "classes_", []))
            return [str(classes[i]) for i, p in enumerate(proba) if p >= 0.42 and i < len(classes)]
        pred = model.predict(X)
        return [str(pred[0])] if len(pred) else []
    except Exception:
        return []


def maybe_train_lesson_model(state_dir: Path, *, min_rows: int = 40) -> bool:
    path = state_dir / LESSONS_JSONL
    if not path.is_file():
        return False
    rows: list[tuple[list[float], str]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
            if not line.strip():
                continue
            row = json.loads(line)
            tags = row.get("tags") or []
            if not tags:
                continue
            rows.append((_feature_row({**row, **(row.get("entry_meta") or {})}), str(tags[0])))
    except Exception:
        return False
    if len(rows) < min_rows:
        return False
    try:
        import joblib
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier

        X = np.array([r[0] for r in rows], dtype=np.float64)
        y = np.array([r[1] for r in rows])
        model = RandomForestClassifier(n_estimators=60, max_depth=6, random_state=42)
        model.fit(X, y)
        joblib.dump(model, state_dir / MODEL_FILE)
        (state_dir / META_FILE).write_text(
            json.dumps({"trained_at": time.time(), "samples": len(rows), "classes": sorted(set(y))}, indent=2),
            encoding="utf-8",
        )
        log.info("trade lesson model trained on %d closes", len(rows))
        return True
    except Exception:
        log.debug("trade lesson model train failed", exc_info=True)
        return False


def analyze_close(record: dict[str, Any]) -> TradeLesson:
    """Extract positives, negatives, and lesson tags from one outcome."""
    now = time.time()
    symbol = str(record.get("symbol") or "")
    side = str(record.get("side") or "")
    outcome = str(record.get("outcome") or "neutral")
    roe = record.get("roe_pct")
    roe_f = float(roe) if roe is not None else None
    pnl = record.get("fill_pnl")
    pnl_f = float(pnl) if pnl is not None else None
    win = outcome == "win" or int(record.get("win", 0)) == 1
    loss = outcome == "loss" or (roe_f is not None and roe_f < -3.0 and not win)

    run_label = str(record.get("run_label") or "")
    chop = float(record.get("chop_index") or 0)
    path_eff = float(record.get("path_efficiency") or 0)
    run_score = float(record.get("run_score") or 0)
    curve = str(record.get("curve_phase") or "")
    reason = str(record.get("reason") or "")

    positive: list[str] = []
    negative: list[str] = []
    tags: list[str] = []
    takeaway = ""

    if win:
        if roe_f is not None and roe_f >= 30.0:
            positive.append(f"strong_tp roe={roe_f:+.1f}%")
            tags.append("reward_runner")
        elif roe_f is not None and roe_f >= 8.0:
            positive.append(f"solid_win roe={roe_f:+.1f}%")
        if run_label == "runner" and path_eff >= 0.28:
            positive.append(f"runner_worked path={path_eff:.0%}")
            tags.append("reward_runner")
        if "tp" in reason.lower() or "harvest" in reason.lower():
            positive.append("exchange_tp_discipline")
    else:
        if run_label == "choppy" or (chop >= 0.5 and path_eff < 0.28):
            negative.append(f"choppy_entry chop={chop:.0%} path={path_eff:.0%}")
            tags.append("punish_choppy")
        if roe_f is not None and roe_f <= -25.0:
            negative.append(f"heavy_loss roe={roe_f:.1f}%")
            tags.append("punish_symbol")
        if "sl" in reason.lower():
            negative.append("stop_hit")
        chase = float(record.get("vwap_distance_pct") or record.get("chase_pct") or 0)
        if abs(chase) > 0.012:
            negative.append(f"chase_entry {chase:.2%}")
            tags.append("punish_chase")
        spread = float(record.get("spread_pct") or record.get("book_spread_pct") or 0)
        if spread > 0.0012:
            negative.append(f"wide_spread {spread:.3%}")
            tags.append("punish_wide_spread")
        if curve in ("declining", "stress"):
            negative.append(f"curve_{curve}")
            tags.append("stress_caution")

    if not win and not loss:
        takeaway = "neutral scratch — no gate change"
    elif win:
        takeaway = "repeat what worked: " + (positive[0] if positive else "clean execution")
    else:
        takeaway = "avoid: " + (negative[0] if negative else "weak setup")

    return TradeLesson(
        ts=now,
        symbol=symbol,
        side=side,
        outcome=outcome,
        roe_pct=roe_f,
        pnl_usd=pnl_f,
        positive=positive,
        negative=negative,
        takeaway=takeaway,
        tags=tags,
        entry_meta={
            "run_label": run_label,
            "chop_index": chop,
            "path_efficiency": path_eff,
            "run_score": run_score,
            "curve_phase": curve,
            "signal_score": record.get("signal_score"),
            "pick_score": record.get("pick_score"),
            "reason": reason,
        },
    )


def _apply_tuning_nudge(settings: "Settings", lesson: TradeLesson, active: dict[str, Any]) -> list[str]:
    from scalp_optimizer import ScalpOptimizer

    actions: list[str] = []
    opt = ScalpOptimizer(settings.state_dir, settings)
    t = opt.tuning
    now = time.time()

    if lesson.outcome == "win" and (lesson.roe_pct or 0) >= 15.0:
        t.confluence_delta = max(-0.10, t.confluence_delta - 0.004)
        t.min_score_delta = max(-0.08, t.min_score_delta - 0.003)
        actions.append("nudge_loosen_after_win")
    elif lesson.outcome == "loss":
        t.confluence_delta = min(0.10, t.confluence_delta + 0.006)
        t.ml_conf_delta = min(0.08, t.ml_conf_delta + 0.004)
        if "punish_choppy" in lesson.tags:
            t.volume_delta = min(0.15, t.volume_delta + 0.02)
            actions.append("nudge_volume_after_choppy_loss")
        actions.append("nudge_tighten_after_loss")

    if active.get("loss_streak", 0) >= 3:
        t.confluence_delta = min(0.12, t.confluence_delta + 0.01)
        actions.append("streak_loss_tighten")

    if active.get("win_streak", 0) >= 3 and (lesson.roe_pct or 0) > 0:
        t.confluence_delta = max(-0.12, t.confluence_delta - 0.008)
        actions.append("streak_win_loosen")

    note = f"lesson:{lesson.symbol.split('/')[0]}"
    t.notes = (t.notes + "; " + note).strip("; ")
    opt._save()
    active["tuning_nudge"] = {
        "confluence_delta": t.confluence_delta,
        "ml_conf_delta": t.ml_conf_delta,
        "volume_delta": t.volume_delta,
    }
    active["tuning_until"] = now + 1800.0
    return actions


def _apply_cooldowns(settings: "Settings", lesson: TradeLesson) -> list[str]:
    from cooldowns import SymbolCooldowns

    actions: list[str] = []
    cd_sec = int(getattr(settings, "scalp_cooldown_minutes", 2) * 60)
    cd = SymbolCooldowns(settings.state_dir / "cooldowns.json", max(60, cd_sec))
    sym = lesson.symbol
    if lesson.outcome == "win":
        cd.mark_win(sym)
        actions.append("cooldown_win")
    elif lesson.outcome == "loss":
        cd.mark_loss(sym)
        actions.append("cooldown_loss")
        if (lesson.roe_pct or 0) <= -20.0 or "punish_symbol" in lesson.tags:
            extra = 900 if (lesson.roe_pct or 0) > -40 else 1800
            cd.block(sym, seconds=extra)
            actions.append(f"symbol_block_{extra}s")
    return actions


def _queue_ml_refit(state_dir: Path, *, reason: str) -> None:
    (state_dir / INSTANT_ML_FLAG).write_text(
        json.dumps({"ts": time.time(), "reason": reason}, indent=2),
        encoding="utf-8",
    )


def apply_lesson(settings: "Settings", record: dict[str, Any], lesson: TradeLesson) -> list[str]:
    """Apply lesson actions immediately (same process, no restart)."""
    if not getattr(settings, "trade_lessons_enabled", True):
        return []

    state_dir = settings.state_dir
    active = _load_active(state_dir)
    applied: list[str] = []

    win = lesson.outcome == "win"
    if win:
        active["win_streak"] = int(active.get("win_streak", 0)) + 1
        active["loss_streak"] = 0
    elif lesson.outcome == "loss":
        active["loss_streak"] = int(active.get("loss_streak", 0)) + 1
        active["win_streak"] = 0

    ml_tags = _predict_tags_ml(state_dir, record)
    for tag in ml_tags:
        if tag not in lesson.tags:
            lesson.tags.append(tag)

    applied.extend(_apply_cooldowns(settings, lesson))
    applied.extend(_apply_tuning_nudge(settings, lesson, active))

    blocks = active.setdefault("symbol_blocks", {})
    if lesson.outcome == "loss" and (lesson.roe_pct or 0) <= -15.0:
        until = time.time() + 1200.0
        blocks[lesson.symbol] = {"until": until, "reason": lesson.takeaway[:80]}
        applied.append("symbol_block_active")

    if lesson.outcome == "loss" and lesson.entry_meta.get("run_label") == "choppy":
        pb = active.setdefault("pattern_blocks", {})
        key = f"choppy:{lesson.symbol}"
        pb[key] = {"until": time.time() + 2400.0, "reason": "choppy_loss"}
    if lesson.outcome == "loss" and "punish_chase" in lesson.tags:
        pb = active.setdefault("pattern_blocks", {})
        pb["chase"] = {"until": time.time() + 1800.0, "reason": "chase_loss"}
    if lesson.outcome == "loss" and "punish_wide_spread" in lesson.tags:
        pb = active.setdefault("pattern_blocks", {})
        pb["spread"] = {"until": time.time() + 1200.0, "reason": "wide_spread_loss"}

    min_new = int(getattr(settings, "trade_lesson_ml_refit_every", 2))
    active["closes_since_refit"] = int(active.get("closes_since_refit", 0)) + 1
    if active["closes_since_refit"] >= min_new:
        _queue_ml_refit(state_dir, reason="trade_lesson_batch")
        active["closes_since_refit"] = 0
        applied.append(f"ml_instant_refit_every_{min_new}")

    recent = active.setdefault("recent", [])
    recent.insert(
        0,
        {
            "ts": lesson.ts,
            "symbol": lesson.symbol.split("/")[0],
            "outcome": lesson.outcome,
            "takeaway": lesson.takeaway,
            "actions": applied,
        },
    )
    active["recent"] = recent[:12]
    lesson.actions = applied
    _save_active(state_dir, active)

    if len(recent) % 10 == 0:
        maybe_train_lesson_model(state_dir)

    log.warning(
        "TRADE LESSON %s %s %s | %s | applied: %s",
        lesson.symbol.split("/")[0],
        lesson.side,
        lesson.outcome,
        lesson.takeaway,
        " | ".join(applied[:6]),
    )
    return applied


def on_trade_close(settings: "Settings", record: dict[str, Any] | None) -> TradeLesson | None:
    """Main entry: called synchronously when a trade closes."""
    if record is None or not getattr(settings, "trade_lessons_enabled", True):
        return None
    lesson = analyze_close(record)
    apply_lesson(settings, record, lesson)
    _append_lesson(settings.state_dir, lesson)
    return lesson


def symbol_blocked(settings: "Settings", symbol: str) -> tuple[bool, str]:
    active = _load_active(settings.state_dir)
    now = time.time()
    block = (active.get("symbol_blocks") or {}).get(symbol)
    if block and float(block.get("until", 0)) > now:
        return True, str(block.get("reason") or "recent loss lesson")
    return False, ""


def pattern_blocked(settings: "Settings", symbol: str, *, run_label: str = "", is_choppy: bool = False) -> tuple[bool, str]:
    active = _load_active(settings.state_dir)
    now = time.time()
    for key, block in (active.get("pattern_blocks") or {}).items():
        if float(block.get("until", 0)) <= now:
            continue
        if key == f"choppy:{symbol}" and (run_label == "choppy" or is_choppy):
            return True, str(block.get("reason") or "choppy pattern lesson")
    return False, ""


def entry_blocked_by_lessons(
    settings: "Settings",
    symbol: str,
    side: str,
    *,
    run_label: str = "",
    is_choppy: bool = False,
    chase_pct: float = 0.0,
    spread_pct: float = 0.0,
) -> tuple[bool, str]:
    if not getattr(settings, "trade_lessons_enabled", True):
        return False, ""
    ok, reason = symbol_blocked(settings, symbol)
    if ok:
        return True, f"lesson block: {reason}"
    ok, reason = pattern_blocked(settings, symbol, run_label=run_label, is_choppy=is_choppy)
    if ok:
        return True, f"lesson pattern: {reason}"
    active = _load_active(settings.state_dir)
    now = time.time()
    for key, block in (active.get("pattern_blocks") or {}).items():
        if float(block.get("until", 0)) <= now:
            continue
        if key == "chase" and abs(chase_pct) > 0.008:
            return True, str(block.get("reason") or "chase lesson block")
        if key == "spread" and spread_pct > 0.001:
            return True, str(block.get("reason") or "spread lesson block")
    return False, ""


def active_summary(state_dir: Path) -> dict[str, Any]:
    return _load_active(state_dir)
