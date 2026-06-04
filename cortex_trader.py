"""
Mission-aware context for live LLM trading policy (not chat).

Feeds the policy model equity path, mission pressure, cortex stats, and book state
so decisions align with maintain/exceed +10%/day — not generic finance advice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from growth_optimizer import _day_start_equity
from mission_config import TARGET_DAILY_GROWTH_PCT, progress_toward_daily_goal_pct, sole_objective_label

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def build_policy_context(
    *,
    state_dir: Path,
    symbol: str,
    equity: float | None = None,
    open_count: int | None = None,
) -> dict[str, Any]:
    """Compact mission + book snapshot for llm_policy JSON requests."""
    snap = _read_json(state_dir / "account_snapshot.json", {}) or {}
    eq = float(equity if equity is not None else snap.get("equity") or 0)
    free = float(snap.get("free_margin") or 0)
    opens = int(open_count if open_count is not None else snap.get("open_count") or 0)

    growth_raw = _read_json(state_dir / "growth_metrics.json", {}) or {}
    hist = growth_raw.get("history") or []
    day_start = _day_start_equity(hist, eq)
    today_pct = (eq / day_start - 1.0) * 100.0 if day_start > 0 and eq > 0 else 0.0
    mission: dict[str, Any] = {
        "sole_objective": sole_objective_label(),
        "target_daily_growth_pct": TARGET_DAILY_GROWTH_PCT,
        "equity_usd": round(eq, 4),
        "free_margin_usd": round(free, 4),
        "open_positions": opens,
        "today_growth_pct": round(today_pct, 4),
        "progress_today_pct": round(progress_toward_daily_goal_pct(today_pct), 4),
    }

    if growth_raw:
        mission.update(
            {
                "on_track": growth_raw.get("on_track"),
                "required_daily_return_pct": growth_raw.get("required_daily_return_pct"),
                "trades_per_hour": growth_raw.get("trades_per_hour"),
            }
        )

    hourly = _read_json(state_dir / "hourly_report.json", {}) or {}
    if hourly:
        mission["hourly_optimizer"] = (hourly.get("tuning") or {}).get("action")

    curve = _read_json(state_dir / "pnl_curve.json", {}) or {}
    if curve:
        mission["curve_phase"] = curve.get("last_phase") or curve.get("curve_phase")
        mission["curve_verticality"] = curve.get("last_verticality")
        mission["account_curve_goal"] = "maximize_and_hold_vertical"

    growth = growth_raw
    mission["directive"] = growth.get("directive") or hourly.get("directive")
    if not mission.get("directive") and growth.get("on_track") is False:
        mission["directive"] = "steepen_account_curve"
    if curve.get("preserve_capital"):
        mission["entry_allowed"] = mission.get("entry_allowed", True) and not curve.get(
            "preserve_capital"
        )

    positions: list[dict[str, Any]] = []
    reg = _read_json(state_dir / "position_registry.json", {}) or {}
    for sym, row in list((reg.get("positions") or reg).items())[:12]:
        if not isinstance(row, dict):
            continue
        if isinstance(sym, str) and "/" not in sym and len(sym) < 20:
            pass
        positions.append(
            {
                "symbol": sym if isinstance(sym, str) else row.get("symbol"),
                "side": row.get("side"),
                "roe_pct": row.get("roe_pct"),
                "stop_pct": row.get("stop_pct"),
            }
        )

    cortex: dict[str, Any] = {}
    stats_path = state_dir / "cortex" / "stats.json"
    if stats_path.is_file():
        cortex = _read_json(stats_path, {}) or {}

    return {
        "symbol_focus": symbol,
        "mission": mission,
        "open_book_sample": positions[:8],
        "cortex_stats": cortex,
        "doctrine": [
            "3R scalper: exchange SL/TP only; steward harvests winners on runners.",
            "Never flip-close on signal alone.",
            "Prefer steady directional runners; avoid choppy up/down coins.",
            "Mission: steepen dashboard account curve (+10%/day floor), take profit, stay vertical.",
        ],
    }
