#!/usr/bin/env python3
"""
God Bot Dashboard API — local-only; Blofin secrets never leave this process.

Serves JSON for the React dashboard from exchange + state/ + logs/.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

from config import load_settings
from dashboard_live import (
    bind_dashboard_helpers,
    closed_trades_list,
    get_live_hub,
    parse_ts,
    trades_stream_version,
)
from exchange_client import BlofinExchange
from mission_config import (
    TARGET_DAILY_GROWTH_PCT,
    progress_toward_daily_goal_pct,
    sole_objective_label,
)

app = Flask(__name__, static_folder="dashboard/dist", static_url_path="")
sock = Sock(app)
STATE_DIR = ROOT / "state"
LOG_FILE = ROOT / "logs" / "bot.log"
API_VERSION = "2026.05.30"

_TICKER_CACHE: dict[str, Any] = {"ts": 0.0, "rows": []}
_EQUITY_RANGE_SEC = {
    "H2": 2 * 3600,
    "H3": 3 * 3600,
    "H6": 6 * 3600,
    "H12": 12 * 3600,
    "1D": 86400,
    "3D": 3 * 86400,
    "1W": 604800,
    "1M": 2592000,
    "3M": 7776000,
    "6M": 15552000,
}
_RANGE_ALIASES = {
    "1/12": "H2",
    "112": "H2",
    "2H": "H2",
    "1/8": "H3",
    "18": "H3",
    "3H": "H3",
    "1/4": "H6",
    "14": "H6",
    "6H": "H6",
    "1/2": "H12",
    "12H": "H12",
    "HALFDAY": "H12",
    "H12": "H12",
    "H6": "H6",
    "H3": "H3",
    "H2": "H2",
    "1D": "1D",
    "1DAY": "1D",
    "3D": "3D",
    "3DAY": "3D",
    "1W": "1W",
    "1WEEK": "1W",
    "1M": "1M",
    "1MONTH": "1M",
    "3M": "3M",
    "3MONTH": "3M",
    "6M": "6M",
    "6MONTH": "6M",
    "ALL": "ALL",
    "ALLTIME": "ALL",
    "ALL_TIME": "ALL",
}
_LIMIT_BY_RANGE = {
    "H2": 480,
    "H3": 540,
    "H6": 720,
    "H12": 960,
    "1D": 400,
    "3D": 480,
    "1W": 560,
    "1M": 640,
    "3M": 800,
    "6M": 1000,
    "ALL": 1200,
}
_TICKER_TTL = 180.0


def normalize_equity_range(raw: str | None) -> str:
    key = (raw or "ALL").strip().upper().replace(" ", "").replace("_", "")
    return _RANGE_ALIASES.get(key, "ALL")


def default_limit_for_range(eq_range: str) -> int:
    return _LIMIT_BY_RANGE.get(eq_range, 800)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl(path: Path, limit: int = 100) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def _downsample_series(rows: list[dict], limit: int) -> list[dict]:
    n = len(rows)
    if n <= limit:
        return rows
    if limit < 2:
        return rows[-1:]
    step = (n - 1) / (limit - 1)
    return [rows[int(round(i * step))] for i in range(limit)]


def _account_equity_anchor(*, live_equity: float | None = None) -> float:
    """Best estimate of true account size for tick filtering (ignores micro-glitch samples)."""
    if live_equity and live_equity > 0:
        return float(live_equity)
    snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
    snap_eq = float(snap.get("equity") or 0)
    if snap_eq > 0:
        return snap_eq
    fluid = _read_json(STATE_DIR / "fluid_state.json", {}) or {}
    for key in ("peak_equity", "last_equity"):
        val = float(fluid.get(key) or 0)
        if val > 0:
            return val
    samples = fluid.get("samples") or []
    for item in reversed(samples):
        try:
            val = float(item[1] if isinstance(item, (list, tuple)) else item)
        except (TypeError, ValueError, IndexError):
            continue
        if val >= 20:
            return val
    return 0.0


def _sanitize_equity_ticks(
    rows: list[dict[str, float]], *, live_equity: float | None = None
) -> list[dict[str, float]]:
    """Drop zeros and sustained micro-glitch samples; keep ramp-up and real history."""
    clean = [r for r in rows if float(r.get("equity") or 0) > 0]
    if len(clean) < 3:
        return clean

    anchor = _account_equity_anchor(live_equity=live_equity)
    global_hi = max(float(r["equity"]) for r in clean)
    ref = anchor if anchor >= 20 else global_hi

    first_real_ts: float | None = None
    for r in clean:
        if float(r["equity"]) >= max(80.0, ref * 0.45 if ref >= 80 else 80.0):
            first_real_ts = float(r["ts"])
            break
    ramp_start = (first_real_ts - 6 * 3600) if first_real_ts else 0.0
    ramp_end = (first_real_ts + 12 * 3600) if first_real_ts else 0.0

    fluid = _read_json(STATE_DIR / "fluid_state.json", {}) or {}
    peak = float(fluid.get("peak_equity") or 0)
    hi_cap = max(global_hi * 1.15, (peak * 1.12) if peak > 0 else global_hi * 1.15, ref * 1.4 + 50.0)

    kept: list[dict[str, float]] = []
    for r in clean:
        eq = float(r["equity"])
        ts = float(r["ts"])
        if eq > hi_cap:
            continue
        if first_real_ts and ramp_start <= ts <= ramp_end:
            kept.append(r)
            continue
        if ref >= 120 and eq < max(25.0, ref * 0.12):
            continue
        if global_hi >= 200 and eq < 50:
            continue
        kept.append(r)

    if len(kept) >= max(5, len(clean) // 80):
        return kept

    substantial = sorted(float(r["equity"]) for r in clean if r["equity"] >= 15)
    if len(substantial) >= 5:
        median = substantial[len(substantial) // 2]
    else:
        tail = sorted(float(r["equity"]) for r in clean[-400:])
        median = tail[len(tail) // 2]
    hi = max(median * 2.25, median + 1.5)
    lo = max(0.01, median * 0.4)
    if peak > median:
        hi = min(hi, max(peak * 1.12, median * 1.85))
    return [r for r in clean if lo <= float(r["equity"]) <= hi]


def _append_live_equity_tick(
    rows: list[dict[str, float]], live_equity: float | None
) -> list[dict[str, float]]:
    if live_equity is None or live_equity <= 0:
        return rows
    now = time.time()
    out = list(rows)
    if out and now - out[-1]["ts"] < 2 and abs(out[-1]["equity"] - live_equity) < 1e-6:
        out[-1] = {"ts": now, "equity": round(live_equity, 6)}
        return out
    out.append({"ts": now, "equity": round(live_equity, 6)})
    return out


def _load_equity_ticks_raw(*, live_equity: float | None = None) -> list[dict[str, float]]:
    path = STATE_DIR / "equity_ticks.jsonl"
    if not path.is_file():
        rows: list[dict[str, float]] = []
    else:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                rows.append({"ts": float(raw["ts"]), "equity": float(raw["equity"])})
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    if rows:
        first_positive = next((i for i, r in enumerate(rows) if r["equity"] > 0), None)
        if first_positive is not None and first_positive > 0:
            rows = rows[first_positive:]
        rows = _sanitize_equity_ticks(rows, live_equity=live_equity)
    rows = _append_live_equity_tick(rows, live_equity)
    return rows


def build_pnl_curve_payload(
    *,
    eq_range: str = "ALL",
    limit: int = 800,
    live_equity: float | None = None,
) -> dict[str, Any]:
    """Shared equity curve JSON for REST + live WebSocket snapshot."""
    eq_range = normalize_equity_range(eq_range)
    all_ticks = _load_equity_ticks_raw(live_equity=live_equity)
    cutoff_ts: float | None = None
    ticks_in_range = all_ticks
    if eq_range in _EQUITY_RANGE_SEC:
        cutoff_ts = time.time() - _EQUITY_RANGE_SEC[eq_range]
        ticks_in_range = [t for t in all_ticks if t["ts"] >= cutoff_ts]
        all_ticks = ticks_in_range
    cap = min(max(int(limit), 50), 2000)
    from equity_ticks import resample_for_chart

    now_ts = time.time()
    win_sec = _EQUITY_RANGE_SEC.get(eq_range) if eq_range in _EQUITY_RANGE_SEC else None
    if eq_range == "ALL":
        win_sec = _EQUITY_RANGE_SEC["6M"]
    equity = (
        resample_for_chart(
            all_ticks,
            eq_range,
            cap,
            live_equity=live_equity,
            window_sec=float(win_sec) if win_sec else None,
        )
        if all_ticks
        else []
    )
    chart_window: dict[str, float] | None = None
    if win_sec and equity:
        chart_window = {
            "start_ts": now_ts - float(win_sec),
            "end_ts": now_ts,
        }
    if len(equity) == 1:
        p0 = equity[0]
        span_pad = 90.0 if eq_range in _EQUITY_RANGE_SEC and _EQUITY_RANGE_SEC[eq_range] <= 43200 else 60.0
        equity = [
            p0,
            {"ts": float(p0["ts"]) + span_pad, "equity": float(p0["equity"])},
        ]

    now = datetime.now(timezone.utc)
    day_start_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    session_start_ts = time.time() - 86400.0
    day_baseline = _baseline_equity_at(all_ticks, day_start_ts)
    session_baseline = _baseline_equity_at(all_ticks, session_start_ts)
    range_baseline = all_ticks[0]["equity"] if all_ticks else None
    current_equity = equity[-1]["equity"] if equity else live_equity
    if live_equity and live_equity > 0:
        current_equity = live_equity

    pnl_curve = _read_json(STATE_DIR / "pnl_curve.json", {}) or {}
    fluid = _read_json(STATE_DIR / "fluid_state.json", {}) or {}
    peak_equity = float(
        pnl_curve.get("peak_equity")
        or fluid.get("peak_equity")
        or (max((r["equity"] for r in all_ticks), default=0))
    )

    realized_full = _load_realized_curve()
    realized = realized_full
    if cutoff_ts is not None:
        before = [r for r in realized_full if r["ts"] < cutoff_ts]
        offset = before[-1]["cumulative_pnl"] if before else 0.0
        realized = [
            {
                "ts": r["ts"],
                "cumulative_pnl": round(r["cumulative_pnl"] - offset, 6),
            }
            for r in realized_full
            if r["ts"] >= cutoff_ts
        ]
    total_realized = realized[-1]["cumulative_pnl"] if realized else 0.0

    def _delta(base: float | None) -> float | None:
        if base is None or current_equity is None:
            return None
        return round(float(current_equity) - base, 6)

    return {
        "equity": equity,
        "chart_window": chart_window,
        "realized": realized,
        "baselines": {
            "day_equity": day_baseline,
            "session_equity": session_baseline,
            "peak_equity": round(peak_equity, 6) if peak_equity > 0 else None,
        },
        "summary": {
            "current_equity": round(float(current_equity), 6) if current_equity else None,
            "total_realized_pnl": round(total_realized, 6),
            "pnl_vs_day": _delta(day_baseline),
            "pnl_vs_session": _delta(session_baseline),
            "pnl_vs_range": _delta(range_baseline),
            "range_start_equity": (
                round(float(range_baseline), 6) if range_baseline is not None else None
            ),
            "drawdown_from_peak_pct": (
                round(max(0.0, (peak_equity - float(current_equity)) / peak_equity * 100.0), 2)
                if peak_equity and current_equity is not None and peak_equity > 0
                else None
            ),
            "curve_phase": pnl_curve.get("last_phase"),
            "verticality": pnl_curve.get("last_verticality"),
            "point_count": len(equity),
            "tick_count_raw": len(ticks_in_range),
            "trade_count": len(realized),
        },
        "range": eq_range,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_realized_curve() -> list[dict[str, float]]:
    prof = _read_json(STATE_DIR / "profitability.json", {"trades": []}) or {"trades": []}
    trades = sorted(prof.get("trades") or [], key=lambda t: float(t.get("ts") or 0))
    cum = 0.0
    out: list[dict[str, float]] = []
    for t in trades:
        pnl = float(t.get("net_pnl") or t.get("pnl_usd") or 0)
        cum += pnl
        out.append({"ts": float(t.get("ts") or 0), "cumulative_pnl": round(cum, 6)})
    return out


def _baseline_equity_at(ticks: list[dict[str, float]], cutoff_ts: float) -> float | None:
    for row in ticks:
        if row["ts"] >= cutoff_ts:
            return row["equity"]
    return ticks[-1]["equity"] if ticks else None


def _bot_running() -> bool:
    try:
        from whatsapp_agent import is_bot_running

        return is_bot_running()
    except Exception:
        return False


def _symbol_short(sym: str) -> str:
    return sym.split("/")[0] if "/" in sym else sym.replace(":USDT", "")


def _parse_log_signals(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    pat = re.compile(
        r"ML SIGNAL\s+(\S+)\s+(long|short)\s+.*?"
        r"score=([\d.]+).*?conf=([\d.]+).*?lev=(\d+)x",
        re.I,
    )
    for line in lines:
        m = pat.search(line)
        if not m:
            continue
        out.append(
            {
                "symbol": m.group(1),
                "side": m.group(2).lower(),
                "score": float(m.group(3)),
                "confidence": float(m.group(4)),
                "leverage": int(m.group(5)),
                "raw": line.strip()[-200:],
            }
        )
    return out


_PICK_PAT = re.compile(
    r"PICK\s+(\S+)\s+(long|short)\s+score=([\d.]+)\s+fast=([\d.]+)\s+tier=(\S+)",
    re.I,
)
_CONFLUENCE_PAT = re.compile(
    r"CONFLUENCE\s+(\S+)\s+(long|short)\s+score=([\d.]+)\s+conf=([\d.]+)\s+cf=([\d.]+)%"
    r".*?zone=\[([^\]]*)\].*?agree=(\d+)\s+oppose=(\d+)\s+lev=(\d+)x",
    re.I,
)
_SCAN_PLAN_PAT = re.compile(
    r"scan plan depth=(\d+)/(\d+)\s+universe\s+\|\s+momentum=(\d+)\s+rotation@(\d+)\s+fresh=(\S+)\s+cov=([\d.]+)%",
    re.I,
)


def _tail_log_lines(limit: int = 2000) -> tuple[list[str], int]:
    """Last N lines from bot.log and current file byte size (for log polling)."""
    if not LOG_FILE.is_file():
        return [], 0
    size = LOG_FILE.stat().st_size
    if size == 0:
        return [], 0
    with LOG_FILE.open("rb") as fh:
        chunk = 65536
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= limit:
            read_at = max(0, pos - chunk)
            fh.seek(read_at)
            data = fh.read(pos - read_at) + data
            pos = read_at
        text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-limit:], size


def _parse_scan_feed(lines: list[str]) -> tuple[list[dict], dict | None]:
    """Merge recent PICK + CONFLUENCE log lines into one row per symbol."""
    by_sym: dict[str, dict] = {}
    scan_plan: dict | None = None

    for line in lines:
        m_plan = _SCAN_PLAN_PAT.search(line)
        if m_plan:
            scan_plan = {
                "depth": int(m_plan.group(1)),
                "universe_n": int(m_plan.group(2)),
                "momentum_slots": int(m_plan.group(3)),
                "rotation_offset": int(m_plan.group(4)),
                "stream_fresh": m_plan.group(5).lower() == "true",
                "ticker_coverage_pct": float(m_plan.group(6)),
            }
            continue

        m_pick = _PICK_PAT.search(line)
        if m_pick:
            sym = m_pick.group(1)
            row = by_sym.setdefault(sym, {"symbol": sym, "symbol_short": _symbol_short(sym)})
            row.update(
                {
                    "side": m_pick.group(2).lower(),
                    "pick_score": float(m_pick.group(3)),
                    "fast_score": float(m_pick.group(4)),
                    "tier": m_pick.group(5),
                    "status": "pick",
                }
            )
            continue

        m_cf = _CONFLUENCE_PAT.search(line)
        if m_cf:
            sym = m_cf.group(1)
            row = by_sym.setdefault(sym, {"symbol": sym, "symbol_short": _symbol_short(sym)})
            row.update(
                {
                    "side": m_cf.group(2).lower(),
                    "score": float(m_cf.group(3)),
                    "confidence": float(m_cf.group(4)),
                    "confluence_pct": float(m_cf.group(5)),
                    "zone": m_cf.group(6),
                    "agree": int(m_cf.group(7)),
                    "oppose": int(m_cf.group(8)),
                    "leverage": int(m_cf.group(9)),
                    "status": "confluence",
                }
            )

    rows = list(by_sym.values())
    for r in rows:
        pick_raw = float(r.get("pick_score") or 0)
        pick_pct = pick_raw * 100.0 if pick_raw <= 1.5 else pick_raw
        fast_raw = float(r.get("fast_score") or 0)
        fast_pct = fast_raw * 100.0 if fast_raw <= 1.5 else fast_raw
        conf_pct = float(r.get("confluence_pct") or 0)
        if conf_pct <= 1.5 and r.get("confidence"):
            conf_pct = float(r["confidence"]) * 100.0
        # CONFLUENCE log uses score=100 (gate pass), not pick strength — keep both.
        if conf_pct <= 0:
            cf_score = float(r.get("score") or 0)
            if 0 < cf_score <= 100:
                conf_pct = cf_score
        r["pick_pct"] = round(pick_pct, 1)
        r["fast_pct"] = round(fast_pct, 1)
        r["score"] = r["pick_pct"]
        r["confluence_pct"] = round(conf_pct, 1) if conf_pct > 0 else None
        if r.get("confidence") is None and conf_pct > 0:
            r["confidence"] = conf_pct / 100.0
        r["rank"] = pick_pct * 0.5 + (conf_pct or 0) * 0.5
        if "tier" not in r:
            r["tier"] = "—"
        if "leverage" not in r:
            r["leverage"] = None

    rows.sort(
        key=lambda x: float(x.get("rank") or 0),
        reverse=True,
    )
    return rows, scan_plan


bind_dashboard_helpers(
    tail_log_lines=_tail_log_lines,
    parse_scan_feed=_parse_scan_feed,
    parse_log_signals=_parse_log_signals,
    read_json=_read_json,
    read_jsonl=_read_jsonl,
    load_equity_ticks_raw=_load_equity_ticks_raw,
    load_realized_curve=_load_realized_curve,
    baseline_equity_at=_baseline_equity_at,
    downsample_series=_downsample_series,
    symbol_short=_symbol_short,
    bot_running=_bot_running,
    equity_range_sec=_EQUITY_RANGE_SEC,
    state_dir=STATE_DIR,
    log_file=LOG_FILE,
)


def _exchange() -> BlofinExchange:
    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    return ex


@app.route("/api/health")
def api_health():
    routes_ok = {
        "scanner": True,
        "logs": True,
        "pnl_curve": True,
    }
    return jsonify(
        {
            "ok": True,
            "version": API_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
            "state_dir": str(STATE_DIR),
            "log_file": str(LOG_FILE),
            "log_exists": LOG_FILE.is_file(),
            "bot_running": _bot_running(),
            "features": ["pnl-curve", "status", "positions", "signals", "scanner", "tickers", "logs", "websocket"],
            "routes": routes_ok,
            "copilot_llm": _copilot_llm_health(),
        }
    )


def _copilot_llm_health() -> dict[str, Any]:
    try:
        from dashboard_copilot import get_copilot_llm_status

        return get_copilot_llm_status()
    except Exception as exc:
        return {"status": "error", "last_error": str(exc)}


@app.route("/api/status")
def api_status():
    """Fast status from local snapshot (bot publishes); never blocks on REST."""
    snap_cache = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
    degraded = False
    err_msg = ""
    try:
        settings = load_settings()
        equity = float(snap_cache.get("equity") or 0)
        free = float(snap_cache.get("free_margin") or equity)
        positions: dict = {}
        pnl_curve = _read_json(STATE_DIR / "pnl_curve.json", {})
        fluid = _read_json(STATE_DIR / "fluid_state.json", {})
        hourly = _read_json(STATE_DIR / "hourly_report.json", {})
        markov = _read_json(STATE_DIR / "markov_regime.json", {})
        self_heal = _read_json(STATE_DIR / "self_heal.json", {})

        exposure = float(snap_cache.get("exposure_usdt") or 0)
        unrealized = float(snap_cache.get("unrealized_pnl") or 0)
        all_ticks = _load_equity_ticks_raw()
        now = datetime.now(timezone.utc)
        day_start_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month_start_ts = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        day_base = _baseline_equity_at(all_ticks, day_start_ts)
        month_base = _baseline_equity_at(all_ticks, month_start_ts)
        session_base = _baseline_equity_at(all_ticks, time.time() - 86400.0)
        today_pct = (
            (equity / day_base - 1.0) * 100.0 if day_base and day_base > 0 and equity > 0 else 0.0
        )
        progress = progress_toward_daily_goal_pct(today_pct)
        daily_pnl = round(equity - day_base, 4) if day_base is not None else None
        monthly_pnl = round(equity - month_base, 4) if month_base is not None else None
        session_pnl = round(equity - session_base, 4) if session_base is not None else None

        return jsonify(
            {
                "mission": sole_objective_label(),
                "target_daily_growth_pct": TARGET_DAILY_GROWTH_PCT,
                "today_growth_pct": round(today_pct, 4),
                "progress_today_pct": round(progress, 4),
                "progress_log_pct": round(progress, 4),
                "equity": round(equity, 4),
                "free_margin": round(free, 4),
                "used_margin": round(max(equity - free, 0), 4),
                "exposure_usdt": round(exposure, 4),
                "unrealized_pnl": round(unrealized, 4),
                "daily_pnl": daily_pnl,
                "monthly_pnl": monthly_pnl,
                "session_pnl": session_pnl,
                "open_count": len(positions) or int(snap_cache.get("open_count") or 0),
                "bot_running": _bot_running(),
                "live": settings.mode == "live" and not settings.dry_run,
                "mode": settings.mode,
                "dry_run": settings.dry_run,
                "curve_phase": pnl_curve.get("last_phase"),
                "verticality": pnl_curve.get("last_verticality"),
                "peak_equity": fluid.get("peak_equity"),
                "trough_equity": fluid.get("trough_equity"),
                "hourly": hourly.get("tuning", {}),
                "markov_ts": markov.get("ts"),
                "self_heal": self_heal,
                "degraded": degraded,
                "error": err_msg or None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:
        eq = float(snap_cache.get("equity") or 0)
        free = float(snap_cache.get("free_margin") or 0)
        opens = int(snap_cache.get("open_count") or 0)
        today_pct = 0.0
        progress = 0.0
        return jsonify(
            {
                "mission": sole_objective_label(),
                "target_daily_growth_pct": TARGET_DAILY_GROWTH_PCT,
                "today_growth_pct": round(today_pct, 4),
                "progress_today_pct": round(progress, 4),
                "progress_log_pct": round(progress, 4),
                "equity": round(eq, 4),
                "free_margin": round(free, 4),
                "used_margin": round(max(eq - free, 0), 4),
                "exposure_usdt": snap_cache.get("exposure_usdt"),
                "unrealized_pnl": snap_cache.get("unrealized_pnl"),
                "open_count": opens,
                "bot_running": _bot_running(),
                "degraded": True,
                "error": str(exc)[:200],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def _snapshot_fingerprint(snap: dict[str, Any]) -> str:
    """Stable hash for WS dedup (ignore stream_ts / log tail noise)."""
    closed = snap.get("closed") or []
    trades_tail = [
        {
            "symbol": t.get("symbol"),
            "ts": t.get("ts"),
            "pnl_usd": t.get("pnl_usd"),
            "event": t.get("event"),
        }
        for t in closed[:5]
    ]
    body = {
        k: snap.get(k)
        for k in (
            "status",
            "positions",
            "active_setups",
            "developing_setups",
            "scanner",
            "pnl_curve",
            "errors",
        )
    }
    body["trades_version"] = snap.get("trades_version") or trades_stream_version()
    body["trades_tail"] = trades_tail
    body["closed_count"] = len(closed)
    return json.dumps(body, sort_keys=True, default=str)


@app.route("/api/live/snapshot")
def api_live_snapshot():
    """Full live hub snapshot (same payload as WebSocket updates)."""
    snap = get_live_hub().get_snapshot()
    return jsonify(snap)


@app.route("/api/positions")
def api_positions():
    try:
        snap = get_live_hub().get_snapshot()
        rows = snap.get("positions") or []
        return jsonify(
            {
                "positions": rows,
                "count": len(rows),
                "stream_ts": snap.get("stream_ts"),
                "errors": snap.get("errors"),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/signals")
def api_signals():
    try:
        snap = get_live_hub().get_snapshot()
        active = snap.get("active_setups") or []
        developing = snap.get("developing_setups") or []
        return jsonify(
            {
                "active_setups": active,
                "developing_setups": developing,
                "recent_scan_count": len(active) + len(developing),
                "stream_ts": snap.get("stream_ts"),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@sock.route("/ws/live")
def ws_live(ws):
    """Push live bot snapshot ~every 1.5s (setups, positions, trades, logs, curve)."""
    hub = get_live_hub()
    try:
        snap = hub.get_snapshot()
        ws.send(json.dumps({"type": "hello", "version": API_VERSION, "data": snap}))
        last_sig = _snapshot_fingerprint(snap)
        while True:
            time.sleep(1.5)
            snap = hub.get_snapshot()
            sig = _snapshot_fingerprint(snap)
            if sig != last_sig:
                ws.send(json.dumps({"type": "update", "data": snap}))
                last_sig = sig
            else:
                ws.send(
                    json.dumps(
                        {"type": "heartbeat", "ts": snap.get("stream_ts"), "data": snap}
                    )
                )
    except Exception:
        pass


@app.route("/api/pnl-curve")
def api_pnl_curve():
    try:
        eq_range = normalize_equity_range(request.args.get("range"))
        raw_limit = request.args.get("limit")
        if raw_limit is not None and str(raw_limit).strip() != "":
            limit = min(max(int(raw_limit), 50), 2000)
        else:
            limit = default_limit_for_range(eq_range)
        live_eq = request.args.get("live_equity")
        live_equity = float(live_eq) if live_eq else None
        return jsonify(
            build_pnl_curve_payload(
                eq_range=eq_range, limit=limit, live_equity=live_equity
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/trades/closed")
def api_closed_trades():
    hours = min(max(int(request.args.get("hours", 0)), 0), 720)
    limit = min(max(int(request.args.get("limit", 80)), 1), 200)
    snap = get_live_hub().get_snapshot()
    if hours > 0:
        closed = closed_trades_list(limit=limit, hours=float(hours))
    else:
        closed = snap.get("closed") or closed_trades_list(limit=limit)
    return jsonify(
        {
            "trades": closed,
            "count": len(closed),
            "hours": hours or None,
            "limit": limit,
            "source": "profitability.json+trade_outcomes.jsonl",
            "trades_version": trades_stream_version(),
            "stream_ts": snap.get("stream_ts"),
            "updated_at": snap.get("closed_updated_at"),
        }
    )


@app.route("/api/tickers")
def api_tickers():
    q = (request.args.get("q") or "").strip().upper()
    sort = (request.args.get("sort") or "change").lower()
    limit = min(int(request.args.get("limit", 500)), 600)
    offset = max(int(request.args.get("offset", 0)), 0)

    now = time.time()
    if now - float(_TICKER_CACHE["ts"]) > _TICKER_TTL or not _TICKER_CACHE["rows"]:
        try:
            ex = _exchange()
            raw = ex.http.list_tickers() or []
            rows: list[dict] = []
            for row in raw:
                inst = row.get("instId") or ""
                if not inst.endswith("-USDT") or "USDT-USDT" in inst:
                    continue
                if row.get("instType") and row.get("instType") != "SWAP":
                    continue
                sym = inst.replace("-USDT", "")
                last = float(row.get("last") or row.get("lastPrice") or 0)
                open_24 = float(row.get("open24h") or row.get("sodUtc0") or last or 0)
                chg = ((last / open_24) - 1) * 100 if open_24 else 0
                vol = float(row.get("vol24h") or row.get("volCcy24h") or 0)
                rows.append(
                    {
                        "symbol": f"{sym}/USDT:USDT",
                        "symbol_short": sym,
                        "last": last,
                        "change_24h_pct": round(chg, 2),
                        "volume_24h": vol,
                    }
                )
            _TICKER_CACHE["rows"] = rows
            _TICKER_CACHE["ts"] = now
        except Exception as exc:
            if _TICKER_CACHE["rows"]:
                log.warning("tickers refresh failed — serving cache: %s", str(exc)[:120])
            else:
                return jsonify({"error": str(exc)}), 500

    rows = list(_TICKER_CACHE["rows"])
    if q:
        rows = [r for r in rows if q in r["symbol_short"].upper() or q in r["symbol"].upper()]

    if sort == "symbol":
        rows.sort(key=lambda r: r["symbol_short"])
    elif sort == "volume":
        rows.sort(key=lambda r: r["volume_24h"], reverse=True)
    else:
        rows.sort(key=lambda r: abs(r["change_24h_pct"]), reverse=True)

    page = rows[offset : offset + limit]
    return jsonify(
        {
            "tickers": page,
            "total": len(rows),
            "universe_total": len(_TICKER_CACHE["rows"]),
            "offset": offset,
            "limit": limit,
        }
    )


@app.route("/api/scanner")
@app.route("/api/scan-feed")
def api_scanner():
    """Structured scan picks from recent bot.log (PICK / CONFLUENCE / scan plan)."""
    try:
        limit = min(max(int(request.args.get("limit", 48)), 6), 120)
        snap = get_live_hub().get_snapshot()
        feed = snap.get("scanner") or {}
        picks = list(feed.get("picks") or [])
        return jsonify(
            {
                "picks": picks[:limit],
                "count": int(feed.get("count") or len(picks)),
                "scan_plan": feed.get("scan_plan"),
                "source": feed.get("source") or "bot.log",
                "updated_at": feed.get("updated_at") or snap.get("stream_ts"),
                "stream_ts": snap.get("stream_ts"),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/logs")
def api_logs():
    n = min(max(int(request.args.get("n", 120)), 20), 500)
    since_raw = request.args.get("since")
    if not LOG_FILE.is_file():
        return jsonify({"lines": [], "count": 0, "offset": 0, "path": str(LOG_FILE)})

    size = LOG_FILE.stat().st_size
    if since_raw is not None:
        try:
            since = max(0, int(since_raw))
        except ValueError:
            return jsonify({"error": "invalid since offset"}), 400
        if since >= size:
            return jsonify({"lines": [], "count": 0, "offset": size, "path": str(LOG_FILE)})
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(since)
            chunk = fh.read()
        lines = chunk.splitlines()
        if since > 0 and chunk and not chunk.startswith("\n") and lines:
            lines = lines[1:]
        return jsonify({"lines": lines, "count": len(lines), "offset": size, "path": str(LOG_FILE)})

    lines, _ = _tail_log_lines(n)
    return jsonify({"lines": lines, "count": len(lines), "offset": size, "path": str(LOG_FILE)})


@app.route("/api/settings")
def api_settings():
    try:
        s = load_settings()
        return jsonify(
            {
                "mode": s.mode,
                "dry_run": s.dry_run,
                "trade_universe": s.trade_universe,
                "leverage": s.leverage,
                "auto_leverage_max": s.auto_leverage_max,
                "max_positions": s.max_positions,
                "poll_seconds": s.poll_seconds,
                "risk_per_trade_pct": s.risk_per_trade_pct,
                "min_signal_score": s.min_signal_score,
                "ml_min_confidence": s.ml_min_confidence,
                "scalp_mode": s.scalp_mode,
                "self_heal_enabled": s.self_heal_enabled,
                "unrestricted_trading": s.unrestricted_trading,
                "state_dir": str(s.state_dir),
                "log_dir": str(s.log_dir),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/chat/llm")
def api_chat_llm():
    try:
        from dashboard_copilot import get_copilot_llm_status

        return jsonify(get_copilot_llm_status())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/chat/history")
def api_chat_history():
    try:
        from dashboard_copilot import get_history_for_ui

        rows = get_history_for_ui()
        return jsonify({"messages": [{"role": r["role"], "content": r["content"]} for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    # Client may send history for UI sync; server persisted history is source of truth.
    _client_history = body.get("history")
    if not message:
        return jsonify({"error": "message required"}), 400
    try:
        from dashboard_copilot import reply_to_message

        reply = reply_to_message(message)
        return jsonify({"reply": reply})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _spawn_fresh_stack_restart() -> None:
    """
    Detached restart: kill every bot.py + dashboard, then start one clean stack.

    Must not be a direct child of dashboard_api — Restart-FreshStack stops this process,
    which would otherwise kill the restart script on Windows.
    """
    helper = ROOT / "scripts" / "stack_restart_detached.ps1"
    if not helper.is_file():
        raise FileNotFoundError(f"missing {helper}")

    port = int(__import__("os").environ.get("DASHBOARD_PORT", "5050"))
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "stack_restart.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} dashboard_api spawn restart port={port}\n")

    # Launch helper directly (not as a child job of this process) so restart-fresh
    # can stop dashboard_api without killing the restart script.
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-DashboardPort",
        str(port),
    ]
    flags = 0
    if sys.platform == "win32":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    subprocess.Popen(
        args,
        cwd=str(ROOT),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


@app.route("/api/stack/<action>", methods=["POST"])
def api_stack(action: str):
    if action not in {"start", "stop", "restart", "restart-fresh", "status"}:
        return jsonify({"error": "invalid action"}), 400
    if action in ("restart", "restart-fresh"):
        try:
            _spawn_fresh_stack_restart()
            return jsonify(
                {
                    "action": action,
                    "ok": True,
                    "async": True,
                    "bot_running": _bot_running(),
                    "output": (
                        "Stack restart launched (detached). Bot + dashboard should return in ~20–40s. "
                        "Log: logs/stack_restart.log — hard-refresh if this page stays offline."
                    ),
                }
            )
        except Exception as exc:
            return jsonify({"action": action, "ok": False, "error": str(exc)}), 500

    ps1 = ROOT / "scripts" / "stack_control.ps1"
    timeout = 45
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Action", action],
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
        )
        text = (out or "").strip()
        running = _bot_running()
        ok = action == "status" or running or "Started bot" in text or "already running" in text.lower()
        if action == "stop":
            ok = "NOT RUNNING" in text or not running
        return jsonify(
            {
                "action": action,
                "ok": ok,
                "bot_running": running,
                "output": text,
            }
        )
    except subprocess.CalledProcessError as exc:
        text = (exc.output or str(exc))[:500]
        return jsonify(
            {
                "action": action,
                "ok": False,
                "bot_running": _bot_running(),
                "error": text,
                "output": text,
            }
        ), 500
    except subprocess.TimeoutExpired:
        return jsonify(
            {
                "action": action,
                "ok": False,
                "bot_running": _bot_running(),
                "error": f"stack {action} timed out after {timeout}s",
            }
        ), 504


@app.route("/")
def index():
    dist = ROOT / "dashboard" / "dist" / "index.html"
    if dist.is_file():
        return send_from_directory(dist.parent, "index.html")
    return (
        "<html><body style='background:#0a0a0f;color:#0ff;font-family:monospace;padding:2rem'>"
        "<h1>God Bot Dashboard API</h1>"
        "<p>API is running. Build the UI: <code>cd dashboard && npm install && npm run build</code></p>"
        "<p>Dev: <code>npm run dev</code> (proxies /api to :5050)</p>"
        "</body></html>"
    )


def main() -> None:
    port = int(__import__("os").environ.get("DASHBOARD_PORT", "5050"))
    load_settings()
    try:
        from dashboard_copilot import prune_legacy_chat_history

        prune_legacy_chat_history()
    except Exception:
        log.debug("chat history prune skipped", exc_info=True)

    try:
        from dashboard_copilot import start_copilot_llm_keeper

        start_copilot_llm_keeper()
        print("Copilot LLM keeper started (warm on boot + periodic keepalive)", flush=True)
    except Exception as exc:
        log.warning("Copilot LLM keeper failed to start: %s", exc)
    get_live_hub()
    print(f"God Bot Dashboard API http://127.0.0.1:{port}")
    print(f"Live WebSocket ws://127.0.0.1:{port}/ws/live")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
