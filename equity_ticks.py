"""Equity tick recording and chart resampling for the account curve."""

from __future__ import annotations

import json
import time
from pathlib import Path

_SUBDAY_RANGES = frozenset({"H2", "H3", "H6", "H12"})

_RANGE_WINDOW_SEC = {
    "H2": 2 * 3600,
    "H3": 3 * 3600,
    "H6": 6 * 3600,
    "H12": 12 * 3600,
}

# Target spacing for chart series (seconds between points)
_CHART_INTERVAL_SEC = {
    "H2": 15,
    "H3": 20,
    "H6": 30,
    "H12": 45,
}

_CHART_POINT_CAP = {
    "H2": 480,
    "H3": 540,
    "H6": 720,
    "H12": 960,
}

_last_append_ts = 0.0
_last_append_equity = 0.0


def _read_last_tick(path: Path) -> tuple[float, float]:
    if not path.is_file():
        return 0.0, 0.0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-8:]):
            if not line.strip():
                continue
            row = json.loads(line)
            return float(row["ts"]), float(row["equity"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return 0.0, 0.0


def append_equity_tick(
    state_dir: Path,
    equity: float,
    *,
    min_interval_sec: float = 10.0,
    min_change_usd: float = 0.25,
    min_change_pct: float = 0.00015,
    api_ok: bool = True,
) -> bool:
    """Append to equity_ticks.jsonl — frequent enough for sub-day charts, without spamming."""
    global _last_append_ts, _last_append_equity
    if not api_ok or equity <= 0:
        return False
    now = time.time()
    path = state_dir / "equity_ticks.jsonl"
    file_ts, file_eq = _read_last_tick(path)
    ref_ts = max(_last_append_ts, file_ts)
    ref_eq = _last_append_equity if _last_append_equity > 0 else file_eq
    if ref_eq >= 50 and equity < ref_eq * 0.20:
        return False
    changed = ref_eq > 0 and abs(equity - ref_eq) >= min_change_usd
    if ref_eq > 0:
        pct_move = abs(equity - ref_eq) / ref_eq
        changed = changed or pct_move >= min_change_pct
    due = ref_ts <= 0 or (now - ref_ts) >= min_interval_sec
    if ref_ts > 0 and not due and not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now, "equity": round(equity, 6)}) + "\n")
        _last_append_ts = now
        _last_append_equity = equity
        return True
    except OSError:
        return False


def resample_for_chart(
    rows: list[dict[str, float]],
    eq_range: str,
    limit: int,
    *,
    live_equity: float | None = None,
    window_sec: float | None = None,
) -> list[dict[str, float]]:
    """Uniform time grid with linear blend for sub-day; index downsample otherwise."""
    if not rows:
        return []
    work = list(rows)
    if live_equity and live_equity > 0:
        now = time.time()
        if not work or now - work[-1]["ts"] >= 1.0:
            work.append({"ts": now, "equity": round(live_equity, 6)})
        elif abs(work[-1]["equity"] - live_equity) >= 1e-6:
            work[-1] = {"ts": now, "equity": round(live_equity, 6)}

    if eq_range not in _SUBDAY_RANGES:
        return _downsample_index(work, limit)

    interval = _CHART_INTERVAL_SEC.get(eq_range, 30)
    cap = min(limit, _CHART_POINT_CAP.get(eq_range, limit))
    end_ts = work[-1]["ts"]
    win = window_sec or _RANGE_WINDOW_SEC.get(eq_range) or (end_ts - work[0]["ts"])
    start_ts = end_ts - max(win, interval)
    src = [r for r in work if r["ts"] >= start_ts - 1.0]
    if not src:
        src = work
    if src and src[0]["ts"] > start_ts + interval:
        src = [{"ts": start_ts, "equity": src[0]["equity"]}, *src]
    n_pts = min(cap, max(32, int(win / interval) + 1))
    if n_pts < 2:
        n_pts = 2
    return _linear_grid(src, start_ts, end_ts, n_pts)


def _downsample_index(rows: list[dict[str, float]], limit: int) -> list[dict[str, float]]:
    n = len(rows)
    if n <= limit:
        return rows
    if limit < 2:
        return rows[-1:]
    step = (n - 1) / (limit - 1)
    return [rows[int(round(i * step))] for i in range(limit)]


def _linear_grid(
    rows: list[dict[str, float]],
    start_ts: float,
    end_ts: float,
    n_points: int,
) -> list[dict[str, float]]:
    if end_ts <= start_ts or n_points < 2:
        return rows[-1:] if rows else []
    out: list[dict[str, float]] = []
    j = 0
    for i in range(n_points):
        t = start_ts + (end_ts - start_ts) * (i / (n_points - 1))
        while j + 1 < len(rows) and rows[j + 1]["ts"] <= t:
            j += 1
        if j + 1 < len(rows) and rows[j + 1]["ts"] > rows[j]["ts"]:
            t0, e0 = rows[j]["ts"], rows[j]["equity"]
            t1, e1 = rows[j + 1]["ts"], rows[j + 1]["equity"]
            if t1 > t0 and t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0)
                eq = e0 + (e1 - e0) * frac
            else:
                eq = e0 if t >= t0 else e1
        else:
            eq = rows[min(j, len(rows) - 1)]["equity"]
        out.append({"ts": t, "equity": round(eq, 6)})
    return out
