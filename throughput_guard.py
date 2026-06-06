"""
Proactive throughput guard — detect low opens/hr early and auto-fix without restart.

Runs from stack_guard (5m), self_heal (in-bot), and hourly_maintain.
Writes scalp_tuning.json, clears stale pauses, nudges TPSL pacer, forces optimizer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

STATE_NAME = "throughput_guard.json"
ACTIONS_NAME = "throughput_guard_actions.jsonl"
MIN_TICK_INTERVAL = 240.0
SEVERE_COOLDOWN = 600.0

SKIP_RE = re.compile(r"WINNER skip [^:]+: (.+)$")
NO_OPEN_RE = re.compile(r"no open: top conv=([\d.]+).*candidates=(\d+)")
STARVED_RE = re.compile(r"STARVED .+ opens=(\d+)/(\d+)")


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_NAME


def _load_state(state_dir: Path) -> dict[str, Any]:
    path = _state_path(state_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state_dir: Path, raw: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    raw["updated_at"] = time.time()
    _state_path(state_dir).write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _append_action(state_dir: Path, row: dict[str, Any]) -> None:
    path = state_dir / ACTIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _target_opens(settings: "Settings") -> int:
    from hourly_3r import target_min_opens_per_hour

    return max(3, target_min_opens_per_hour(settings))


def _tail_log(root: Path, n: int = 1200) -> list[str]:
    path = root / "logs" / "bot.log"
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _classify_skip(reason: str) -> str:
    r = reason.lower()
    if "volume" in r:
        return "volume"
    if "choppy" in r or "chop=" in r:
        return "choppy"
    if "htf" in r:
        return "htf"
    if "conf" in r:
        return "conf"
    if "agreeing" in r:
        return "agreeing"
    if "opposing" in r:
        return "opposing"
    if "vwap" in r or "chasing" in r:
        return "vwap"
    if "funding" in r:
        return "funding"
    if "ml" in r:
        return "ml_disagree"
    if "steady runner" in r or "path=" in r:
        return "runner"
    return "other"


def _analyze_log(lines: list[str]) -> dict[str, Any]:
    skips: Counter[str] = Counter()
    no_open = 0
    last_starved: tuple[int, int] | None = None
    last_no_open: dict[str, Any] = {}

    for line in lines:
        m = SKIP_RE.search(line)
        if m:
            skips[_classify_skip(m.group(1))] += 1
        if "no open:" in line:
            no_open += 1
            nm = NO_OPEN_RE.search(line)
            if nm:
                last_no_open = {
                    "top_conv": float(nm.group(1)),
                    "candidates": int(nm.group(2)),
                }
        sm = STARVED_RE.search(line)
        if sm:
            last_starved = (int(sm.group(1)), int(sm.group(2)))

    dominant = skips.most_common(1)[0][0] if skips else ""
    return {
        "skip_counts": dict(skips),
        "skip_total": sum(skips.values()),
        "dominant_skip": dominant,
        "no_open_count": no_open,
        "last_no_open": last_no_open,
        "last_starved": last_starved,
    }


def _opens_15m(state_dir: Path) -> int:
    from scalp_optimizer import _count_journal_opens

    journal = state_dir / "trades.jsonl"
    return _count_journal_opens(journal, time.time() - 900)


def _tph_trend(state_dir: Path) -> float:
    """Negative = opens/hr declining across recent optimizer reports."""
    path = state_dir / "optimizer_report.jsonl"
    if not path.is_file():
        return 0.0
    rows: list[int] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]:
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append(int(row.get("trades_60m", row.get("tuning", {}).get("trades_last_hour", 0)) or 0))
    except Exception:
        return 0.0
    if len(rows) < 3:
        return 0.0
    return float(rows[-1] - rows[0])


def _apply_tuning_nudge(
    state_dir: Path,
    settings: "Settings",
    *,
    severity: str,
    dominant_skip: str,
    actions: list[str],
) -> None:
    from scalp_optimizer import ScalpOptimizer

    opt = ScalpOptimizer(state_dir, settings)
    t = opt.tuning
    step = 0.012 if severity == "mild" else 0.022

    t.confluence_delta = max(-0.12, t.confluence_delta - step)
    t.agreeing_delta = max(-3, t.agreeing_delta - (1 if severity != "mild" else 0))
    t.ml_conf_delta = max(-0.10, t.ml_conf_delta - step * 0.8)
    t.min_score_delta = max(-0.10, t.min_score_delta - step * 1.1)

    if dominant_skip in ("volume", "runner"):
        t.volume_delta = max(-0.55, t.volume_delta - (0.12 if severity == "mild" else 0.22))
        actions.append(f"nudge_volume_delta={t.volume_delta:.2f}")

    gap = float(t.entry_gap_seconds or settings.scalp_entry_gap_seconds)
    floor = 4.0 if getattr(settings, "tpsl_only_pacing", False) else 6.0
    t.entry_gap_seconds = max(floor, gap - (2.0 if severity == "mild" else 4.0))
    actions.append(f"nudge_entry_gap={t.entry_gap_seconds:.0f}s")

    cd = float(t.symbol_cooldown_minutes or settings.scalp_cooldown_minutes)
    t.symbol_cooldown_minutes = max(1.0, cd - (0.5 if severity == "mild" else 1.0))

    note = f"throughput_guard:{severity}"
    if dominant_skip:
        note += f":{dominant_skip}"
    t.notes = (t.notes + "; " + note).strip("; ")
    opt._save()
    actions.append(f"tuning_nudge_{severity}")


def _ease_pacer(state_dir: Path, settings: "Settings", actions: list[str]) -> None:
    if not getattr(settings, "tpsl_only_pacing", False):
        from hourly_3r import hourly_3r_active

        if not hourly_3r_active(settings):
            return
    path = state_dir / "tpsl_pacer.json"
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    gap = float(raw.get("pending_gap", 0) or 0)
    if gap <= 2.0:
        return
    raw["pending_gap"] = min(gap, 2.0)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    actions.append("pacer_gap_eased")


def _force_throughput_autocode(state_dir: Path, actions: list[str]) -> None:
    from optimizer_autocode import _template

    code = _template("throughput")
    (state_dir / "optimizer_overrides.py").write_text(code, encoding="utf-8")
    (state_dir / "optimizer_autocode_state.json").write_text(
        json.dumps({"mode": "throughput", "ts": time.time(), "source": "throughput_guard"}),
        encoding="utf-8",
    )
    actions.append("autocode_throughput_template")


def tick(
    settings: "Settings",
    *,
    equity: float = 0.0,
    free_margin: float = 0.0,
    opens_60m: int | None = None,
    force: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """
    Analyze throughput health and apply fixes. Safe to call every few minutes.
    """
    state_dir = settings.state_dir
    project_root = root or Path(__file__).resolve().parent
    now = time.time()
    st = _load_state(state_dir)
    last_tick = float(st.get("last_tick", 0))
    if not force and now - last_tick < MIN_TICK_INTERVAL:
        return {"skipped": True, "reason": "cooldown", "next_in_sec": MIN_TICK_INTERVAL - (now - last_tick)}

    target = _target_opens(settings)
    tuning_path = state_dir / "scalp_tuning.json"
    tuning: dict[str, Any] = {}
    if tuning_path.is_file():
        try:
            tuning = json.loads(tuning_path.read_text(encoding="utf-8"))
        except Exception:
            tuning = {}

    tph = opens_60m if opens_60m is not None else int(tuning.get("trades_last_hour", 0) or 0)
    opens_15m = _opens_15m(state_dir)
    log_info = _analyze_log(_tail_log(project_root))
    trend = _tph_trend(state_dir)

    if equity <= 0 and (state_dir / "account_snapshot.json").is_file():
        try:
            snap = json.loads((state_dir / "account_snapshot.json").read_text(encoding="utf-8"))
            equity = float(snap.get("equity") or 0)
            free_margin = float(snap.get("free_margin") or snap.get("free") or 0)
        except Exception:
            pass

    margin_free_pct = (free_margin / equity * 100.0) if equity > 0 else 0.0
    starved = tph < target
    severe = starved and (tph < max(2, target // 2) or (opens_15m == 0 and margin_free_pct > 55))
    predictive = trend <= -2 and tph < target + 1

    actions: list[str] = []
    anomalies: list[str] = []

    if starved:
        anomalies.append(f"opens={tph}/{target}/hr")
    if severe:
        anomalies.append("severe_starvation")
    if predictive:
        anomalies.append(f"tph_trend={trend:+.0f}")
    if log_info["no_open_count"] >= 2:
        anomalies.append(f"no_open_x{log_info['no_open_count']}")
    if log_info["dominant_skip"]:
        anomalies.append(f"blocks={log_info['dominant_skip']}")

    # Clear stale runtime pause when endless flow is configured.
    try:
        from runtime_gates import clear_entries_pause, read_entries_pause

        paused, reason = read_entries_pause(state_dir)
        if paused and (
            getattr(settings, "entries_never_pause", False)
            or equity >= getattr(settings, "micro_equity_threshold", 10.0) * 2.5
        ):
            clear_entries_pause(state_dir)
            actions.append(f"cleared_pause:{reason[:40]}")
    except Exception:
        pass

    last_severe = float(st.get("last_severe_fix", 0))
    severity = ""
    if severe and now - last_severe >= SEVERE_COOLDOWN:
        severity = "severe"
        _apply_tuning_nudge(
            state_dir,
            settings,
            severity="severe",
            dominant_skip=str(log_info.get("dominant_skip") or ""),
            actions=actions,
        )
        _ease_pacer(state_dir, settings, actions)
        _force_throughput_autocode(state_dir, actions)
        st["last_severe_fix"] = now
    elif starved and (log_info["no_open_count"] >= 1 or log_info["skip_total"] >= 40):
        severity = "mild"
        _apply_tuning_nudge(
            state_dir,
            settings,
            severity="mild",
            dominant_skip=str(log_info.get("dominant_skip") or ""),
            actions=actions,
        )
        if log_info["dominant_skip"] in ("volume", "runner") or margin_free_pct > 65:
            _ease_pacer(state_dir, settings, actions)

    if (starved or predictive) and settings.optimizer_enabled and equity > 0:
        try:
            from exchange_client import BlofinExchange
            from scalp_optimizer import ScalpOptimizer

            ex = BlofinExchange(settings)
            ex.load()
            rep = ScalpOptimizer(state_dir, settings).maybe_optimize(
                ex.fetch_equity_usdt(), force=True
            )
            if rep:
                actions.append(f"optimizer:{rep.action}")
            else:
                actions.append("optimizer:skip_interval")
        except Exception as exc:
            actions.append(f"optimizer_err:{exc}")

    report = {
        "ts": now,
        "target_opens_hr": target,
        "opens_60m": tph,
        "opens_15m": opens_15m,
        "margin_free_pct": round(margin_free_pct, 1),
        "starved": starved,
        "severe": severe,
        "predictive": predictive,
        "tph_trend": trend,
        "log": log_info,
        "actions": actions,
        "anomalies": anomalies,
    }

    st["last_tick"] = now
    st["last_report"] = report
    _save_state(state_dir, st)

    if actions:
        _append_action(state_dir, report)
        log.warning(
            "THROUGHPUT GUARD | opens=%d/%d 15m=%d free=%.0f%% | %s",
            tph,
            target,
            opens_15m,
            margin_free_pct,
            " | ".join(actions),
        )
    elif starved or predictive:
        log.info(
            "THROUGHPUT GUARD watch | opens=%d/%d trend=%+.0f dominant=%s",
            tph,
            target,
            trend,
            log_info.get("dominant_skip") or "—",
        )

    return report


def run_standalone() -> int:
    from config import load_settings

    settings = load_settings()
    rep = tick(settings, force=True)
    print(json.dumps(rep, indent=2))
    return 0 if not rep.get("severe") else 1


if __name__ == "__main__":
    raise SystemExit(run_standalone())
