"""24/7 equity-curve health monitor — detects pollution and repairs ticks/peaks."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STATE_NAME = "curve_guard.json"
_LOG_NAME = "curve_guard.log"
_MICRO_CAP_USD = 25.0


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_live_equity(state_dir: Path) -> tuple[float, str]:
    """Best live equity for anchoring repairs."""
    snap = _read_json(state_dir / "account_snapshot.json", {}) or {}
    snap_eq = float(snap.get("equity") or 0)
    if snap_eq > 0 and snap.get("api_ok", True):
        return snap_eq, "snapshot"

    try:
        from config import load_settings
        from exchange_client import BlofinExchange

        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        eq = float(ex.fetch_equity_usdt() or 0)
        if eq > 0:
            return eq, "exchange"
    except Exception as exc:
        log.debug("exchange equity for curve guard failed: %s", exc)

    if snap_eq > 0:
        return snap_eq, "snapshot_stale"

    from dashboard_api import _account_equity_anchor

    anchor = float(_account_equity_anchor() or 0)
    if anchor > 0:
        return anchor, "anchor"
    return 0.0, "none"


def load_equity_ticks(path: Path) -> list[dict[str, float]]:
    if not path.is_file():
        return []
    rows: list[dict[str, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
            rows.append({"ts": float(o["ts"]), "equity": float(o["equity"])})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return rows


def evaluate_curve_health(
    state_dir: Path,
    *,
    live_equity: float | None = None,
) -> dict[str, Any]:
    """Return health report; unhealthy when ticks/peaks drift from live balance."""
    from dashboard_api import _sanitize_equity_ticks, build_pnl_curve_payload

    anchor, anchor_src = (live_equity, "caller") if live_equity and live_equity > 0 else (0.0, "")
    if anchor <= 0:
        anchor, anchor_src = fetch_live_equity(state_dir)

    ticks_path = state_dir / "equity_ticks.jsonl"
    rows = load_equity_ticks(ticks_path)
    issues: list[str] = []
    metrics: dict[str, Any] = {
        "anchor": round(anchor, 6) if anchor > 0 else None,
        "anchor_source": anchor_src,
        "tick_count_raw": len(rows),
    }

    if anchor <= 0:
        issues.append("no_anchor")
    if not rows:
        issues.append("no_ticks")

    recent = rows[-800:] if len(rows) > 800 else rows
    if recent and anchor > 0:
        rmax = max(float(r["equity"]) for r in recent)
        rmin = min(float(r["equity"]) for r in recent)
        metrics["recent_min"] = round(rmin, 6)
        metrics["recent_max"] = round(rmax, 6)
        if anchor < _MICRO_CAP_USD:
            if rmax > anchor * 1.22:
                issues.append("recent_spike")
            if rmin < anchor * 0.50:
                issues.append("stale_low_era")
        else:
            if rmax > anchor * 1.55:
                issues.append("recent_spike")
            if rmin < anchor * 0.30:
                issues.append("stale_low_era")

    kept = _sanitize_equity_ticks(rows, live_equity=anchor if anchor > 0 else None)
    metrics["tick_count_kept"] = len(kept)
    if rows and len(kept) < len(rows) * 0.82:
        issues.append("tick_pollution")

    fluid = _read_json(state_dir / "fluid_state.json", {}) or {}
    fluid_peak = float(fluid.get("peak_equity") or 0)
    if fluid_peak > 0 and anchor > 0:
        cap = anchor * (1.18 if anchor < _MICRO_CAP_USD else 1.35)
        metrics["fluid_peak"] = round(fluid_peak, 6)
        if fluid_peak > cap:
            issues.append("stale_fluid_peak")

    try:
        payload = build_pnl_curve_payload(eq_range="24h", limit=400, live_equity=anchor)
        summary = payload.get("summary") or {}
        baselines = payload.get("baselines") or {}
        chart_eq = payload.get("equity") or []
        current = float(summary.get("current_equity") or anchor or 0)
        peak = float(baselines.get("peak_equity") or 0)
        metrics["chart_current"] = round(current, 6) if current else None
        metrics["chart_peak"] = round(peak, 6) if peak else None
        if chart_eq:
            vals = [float(p["equity"]) for p in chart_eq]
            metrics["chart_min"] = round(min(vals), 6)
            metrics["chart_max"] = round(max(vals), 6)
        if current > 0 and peak > current * (1.18 if current < _MICRO_CAP_USD else 1.25):
            issues.append("chart_peak_drift")
        if chart_eq and current > 0 and current < _MICRO_CAP_USD:
            cmax = max(float(p["equity"]) for p in chart_eq)
            if cmax > current * 1.20:
                issues.append("chart_spike")
    except Exception as exc:
        issues.append("chart_check_failed")
        metrics["chart_error"] = str(exc)[:160]

    return {
        "healthy": len(issues) == 0,
        "issues": issues,
        "metrics": metrics,
        "checked_at": time.time(),
    }


def repair_equity_curve(
    state_dir: Path,
    *,
    live_equity: float | None = None,
) -> dict[str, Any]:
    """Trim ticks and reset peak fields to match live balance."""
    from dashboard_api import _account_equity_anchor, _sanitize_equity_ticks

    path = state_dir / "equity_ticks.jsonl"
    anchor = float(live_equity or 0)
    if anchor <= 0:
        anchor = float(fetch_live_equity(state_dir)[0])
    if anchor <= 0:
        anchor = float(_account_equity_anchor(live_equity=None) or 0)

    rows = load_equity_ticks(path)
    before = len(rows)
    kept = _sanitize_equity_ticks(rows, live_equity=anchor if anchor > 0 else None)
    if kept:
        path.write_text(
            "\n".join(json.dumps(r, separators=(",", ":")) for r in kept)
            + ("\n" if kept else ""),
            encoding="utf-8",
        )

    peak_cap = anchor * (1.18 if anchor < _MICRO_CAP_USD else 1.30) if anchor > 0 else 0.0
    recent_peak = max((float(r["equity"]) for r in kept[-500:]), default=0.0) if kept else 0.0
    if peak_cap > 0:
        recent_peak = min(recent_peak, peak_cap)
    recent_trough = min((float(r["equity"]) for r in kept[-500:]), default=0.0) if kept else 0.0

    fluid_path = state_dir / "fluid_state.json"
    if fluid_path.is_file() and kept:
        fluid = _read_json(fluid_path, {}) or {}
        fluid["peak_equity"] = round(recent_peak, 6)
        fluid["trough_equity"] = round(recent_trough, 6)
        fluid["last_equity"] = round(anchor, 6) if anchor > 0 else fluid.get("last_equity")
        _write_json(fluid_path, fluid)

    pnl_path = state_dir / "pnl_curve.json"
    if pnl_path.is_file() and kept:
        pnl = _read_json(pnl_path, {}) or {}
        pnl["peak_equity"] = round(recent_peak, 6)
        _write_json(pnl_path, pnl)

    return {
        "repaired": True,
        "before": before,
        "after": len(kept),
        "anchor": round(anchor, 6) if anchor > 0 else None,
        "band_min": round(min((r["equity"] for r in kept), default=0.0), 6) if kept else None,
        "band_max": round(max((r["equity"] for r in kept), default=0.0), 6) if kept else None,
        "peak_reset": round(recent_peak, 6) if recent_peak > 0 else None,
    }


def run_guard_tick(
    state_dir: Path,
    *,
    force_repair: bool = False,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """One monitor pass: evaluate, repair if needed, persist status."""
    health = evaluate_curve_health(state_dir)
    repair_result: dict[str, Any] | None = None
    repaired = False

    if force_repair or not health.get("healthy"):
        repair_result = repair_equity_curve(state_dir, live_equity=health.get("metrics", {}).get("anchor"))
        repaired = True
        health = evaluate_curve_health(state_dir)

    status = {
        "updated_at": time.time(),
        "healthy": health.get("healthy", False),
        "issues": health.get("issues", []),
        "metrics": health.get("metrics", {}),
        "repaired": repaired,
        "repair": repair_result,
        "consecutive_failures": 0,
    }

    prev = _read_json(state_dir / _STATE_NAME, {}) or {}
    if not status["healthy"]:
        status["consecutive_failures"] = int(prev.get("consecutive_failures") or 0) + 1
    else:
        status["consecutive_failures"] = 0

    _write_json(state_dir / _STATE_NAME, status)

    msg = (
        f"curve_guard healthy={status['healthy']} issues={status['issues']} "
        f"anchor={status['metrics'].get('anchor')} repaired={repaired}"
    )
    log.info(msg)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {msg}\n")

    # Escalate only after repeated auto-repair failures (optional Cursor handoff).
    flag = state_dir.parent / ".cursor" / "CURVE_REPAIR_DUE"
    if status["consecutive_failures"] >= 6:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            f"triggered_at={time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"issues={','.join(status['issues'])}\n"
            f"consecutive_failures={status['consecutive_failures']}\n"
            f"instruction=Curve guard auto-repair failed repeatedly; fix equity curve sanitization.\n",
            encoding="utf-8",
        )
    elif status["healthy"] and flag.is_file():
        flag.unlink(missing_ok=True)

    return status
