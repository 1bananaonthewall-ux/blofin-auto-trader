"""
Merge growth-agent backtest tuning + learning brain into God Bot (one supercharged process).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
TUNE_PATH = ROOT / "state" / "growth_supercharge.json"
BRAIN_DIR_NAME = "growth_brain"

# Backtest-proven winner (updated by run_growth_agent_backtest.py --apply-supercharge)
DEFAULT_POLICY: dict[str, Any] = {
    "min_confluence": 0.47,
    "min_confidence": 0.55,
    "min_signal_score": 47.0,
    "min_agreeing": 4,
    "skip_choppy": True,
    "margin_pct_per_trade": 2.2,
    "entry_gap_bars": 5,
    "scan_every_bars": 6,
    "poll_seconds_cap": 12,
    "entry_gap_seconds_cap": 30.0,
    "max_margin_deploy_pct": 0.12,
    "confluence_core_mode": True,
    "open_top_only": True,
    "max_opens_per_cycle": 0,
    "scan_top_n": 60,
    "max_daily_loss_pct": 12.0,
    "use_exchange_tpsl_only": True,
    "sl_pct": 1.0,
    "tp_pct": 3.0,
    "source": "default",
}


def enabled(settings: "Settings") -> bool:
    return bool(getattr(settings, "growth_supercharge_enabled", False))


def confluence_core_active(settings: "Settings") -> bool:
    """Use evaluate_entry scan path (backtest-aligned) instead of winner/pick/swarm stack."""
    if not enabled(settings):
        return False
    pol = load_policy(settings.state_dir)
    return bool(pol.get("confluence_core_mode", True))


def daily_loss_pause_active(settings: "Settings", optimizer, equity: float) -> bool:
    if not enabled(settings):
        return False
    pol = load_policy(settings.state_dir)
    cap = float(pol.get("max_daily_loss_pct", 12.0))
    if cap <= 0 or optimizer is None:
        return False
    try:
        from growth_optimizer import _day_start_equity

        day_start = _day_start_equity(optimizer.history, equity)
        if day_start <= 0:
            return False
        day_ret = (equity / day_start - 1.0) * 100.0
        return day_ret <= -cap
    except Exception:
        return False


def policy_path(state_dir: Path) -> Path:
    return state_dir / "growth_supercharge.json"


def brain_dir(state_dir: Path) -> Path:
    return state_dir / BRAIN_DIR_NAME


def load_policy(state_dir: Path) -> dict[str, Any]:
    path = policy_path(state_dir)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {**DEFAULT_POLICY, **raw}
        except Exception:
            pass
    return dict(DEFAULT_POLICY)


def save_policy(state_dir: Path, payload: dict[str, Any]) -> Path:
    path = policy_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_POLICY, **payload, "updated_ts": __import__("time").time()}
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return path


def apply_from_backtest_report(report: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Persist growth backtest winners for God Bot supercharge mode."""
    params = report.get("best_params") or {}
    full = report.get("full_period") or report.get("best_result") or {}
    holdout = report.get("holdout") or {}
    risk = float(params.get("risk_per_trade", 0.022))
    gap_bars = int(params.get("entry_gap_bars", 5))
    scan_bars = int(params.get("scan_every_bars", 6))
    full_wr = full.get("win_rate_pct")
    hold_wr = holdout.get("win_rate_pct")
    row = {
        "min_confluence": float(params.get("min_confluence", 0.47)),
        "min_confidence": float(params.get("min_confidence", 0.55)),
        "min_signal_score": float(params.get("min_composite_score", 47.0)),
        "min_agreeing": int(params.get("min_agreeing", 4)),
        "skip_choppy": bool(params.get("skip_choppy", True)),
        "margin_pct_per_trade": round(risk * 100, 2),
        "entry_gap_bars": gap_bars,
        "scan_every_bars": scan_bars,
        "poll_seconds_cap": max(8, min(20, scan_bars * 2)),
        "entry_gap_seconds_cap": float(gap_bars * 5 * 60),
        "max_margin_deploy_pct": min(0.15, risk * 4.5),
        "confluence_core_mode": True,
        "open_top_only": True,
        "max_opens_per_cycle": 0,
        "scan_top_n": 60,
        "max_daily_loss_pct": 12.0,
        "use_exchange_tpsl_only": True,
        "sl_pct": 1.0,
        "tp_pct": 3.0,
        "backtest_return_pct": full.get("return_pct"),
        "backtest_profit_factor": full.get("profit_factor"),
        "backtest_max_dd_pct": full.get("max_drawdown_pct"),
        "backtest_win_rate_pct": full_wr,
        "holdout_return_pct": holdout.get("return_pct"),
        "holdout_profit_factor": holdout.get("profit_factor"),
        "holdout_win_rate_pct": hold_wr,
        "source": "growth_agent_backtest",
    }
    save_policy(state_dir, row)
    _write_optimizer_overlay(row)
    log.warning(
        "GROWTH SUPERCHARGE applied | conf<=%.2f score<=%.0f margin~%.1f%% | holdout=%.1f%% PF=%.2f",
        row["min_confidence"],
        row["min_signal_score"],
        row["margin_pct_per_trade"],
        float(holdout.get("return_pct") or 0),
        float(holdout.get("profit_factor") or 0),
    )
    return row


def _write_optimizer_overlay(row: dict[str, Any]) -> None:
    """Hot-reload gate cap via optimizer_overrides (live_update picks up without restart)."""
    conf_cap = float(row.get("min_confidence", 0.55))
    score_cap = float(row.get("min_signal_score", 47.0))
    path = ROOT / "optimizer_overrides.py"
    path.write_text(
        f'''"""Growth supercharge gate caps — backtest-tuned."""
def apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0):
    conf = min(conf_gate, {conf_cap:.4f})
    score = min(score_gate, {score_cap:.2f})
    if trades_last_hour < 3:
        conf = min(conf, max(0.52, {conf_cap:.4f}))
        score = min(score, max(45.0, {score_cap:.2f} - 1.0))
    return conf, score
''',
        encoding="utf-8",
    )


def overlay_knobs(
    settings: "Settings",
    *,
    min_conf: float,
    min_score: float,
    deploy_base: float,
    deploy_max: float,
    poll: int,
    entry_gap: float,
    brain=None,
) -> tuple[float, float, float, float, int, float]:
    """Apply backtest-tuned caps + optional brain tighten floors."""
    if not enabled(settings):
        return min_conf, min_score, deploy_base, deploy_max, poll, entry_gap
    pol = load_policy(settings.state_dir)
    min_conf = min(min_conf, float(pol["min_confidence"]))
    min_score = min(min_score, float(pol["min_signal_score"]))
    cap_deploy = float(pol.get("max_margin_deploy_pct", 0.12))
    deploy_base = min(deploy_base, cap_deploy * 0.65)
    deploy_max = min(deploy_max, cap_deploy)
    poll = min(poll, int(pol.get("poll_seconds_cap", 12)))
    entry_gap = min(entry_gap, float(pol.get("entry_gap_seconds_cap", 30.0)))
    if brain is not None:
        b_conf, b_score, margin_mult = brain.effective_gates()
        min_conf = max(min_conf, b_conf)
        min_score = max(min_score, b_score)
        deploy_max = min(deploy_max, cap_deploy * margin_mult)
    return min_conf, min_score, deploy_base, deploy_max, poll, entry_gap


_brain_cache: dict[str, Any] = {}


def get_brain(settings: "Settings"):
    """Shared learning brain under God Bot state (None if supercharge off)."""
    if not enabled(settings):
        return None
    key = str(settings.state_dir)
    if key in _brain_cache:
        return _brain_cache[key]
    from growth_agent_brain import GrowthAgentBrain

    pol = load_policy(settings.state_dir)
    brain = GrowthAgentBrain(
        brain_dir(settings.state_dir),
        base_min_confidence=float(pol["min_confidence"]),
        base_min_score=float(pol["min_signal_score"]),
    )
    _brain_cache[key] = brain
    return brain


def rank_boost(brain, symbol: str, side: str, conviction: float) -> float:
    if brain is None:
        return conviction
    return conviction * brain.rank_boost(symbol, side)


def symbol_blocked(brain, symbol: str, *, run_label: str = "", is_choppy: bool = False) -> tuple[bool, str]:
    if brain is None:
        return False, ""
    return brain.symbol_blocked(symbol, run_label=run_label, is_choppy=is_choppy)
