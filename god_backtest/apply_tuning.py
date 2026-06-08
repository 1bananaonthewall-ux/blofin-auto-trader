"""Apply walk-forward winners to God Bot live + backtest specs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TUNE_PATH = ROOT / "state" / "storefront" / "bot_tune_overrides.json"
APPLIED_PATH = ROOT / "state" / "god_backtest" / "applied_tuning.json"
OPTIMIZER_PATH = ROOT / "optimizer_overrides.py"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_params_to_god_bot(
    params: dict[str, Any],
    *,
    source: str,
    oos_score: float | None = None,
    oos_avg_return: float | None = None,
) -> dict[str, Any]:
    """Merge backtest params into god-bot tune overrides + optimizer relax."""
    row = {
        "min_confluence": float(params.get("min_confluence", 0.48)),
        "min_composite_score": float(params.get("min_composite_score", 49.0)),
        "min_confidence": float(params.get("min_confidence", 0.57)),
        "entry_gap_bars": int(params.get("entry_gap_bars", 6)),
        "require_runner": False,
        "skip_choppy": False,
    }
    if "min_agreeing" in params:
        row["min_agreeing"] = int(params["min_agreeing"])
    if "min_runner_score" in params:
        row["min_runner_score"] = float(params["min_runner_score"])
    if "risk_per_trade" in params:
        row["risk_per_trade"] = float(params["risk_per_trade"])

    tune: dict[str, Any] = {}
    if TUNE_PATH.is_file():
        try:
            tune = json.loads(TUNE_PATH.read_text(encoding="utf-8"))
        except Exception:
            tune = {}
    tune["god-bot"] = {**tune.get("god-bot", {}), **row}
    TUNE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TUNE_PATH.write_text(json.dumps(tune, indent=2), encoding="utf-8")

    # Live gate relax: map backtest confidence/score deltas vs default god-bot base.
    conf_relax = max(0.0, min(0.08, 0.57 - row["min_confidence"]))
    score_relax = max(0.0, min(8.0, 49.0 - row["min_composite_score"]))
    optimizer_code = f'''"""Backtest-tuned gate relax ({source})."""
def apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0):
    conf = max(0.45, conf_gate - {conf_relax:.4f})
    score = max(40.0, score_gate - {score_relax:.2f})
    if trades_last_hour < 4:
        conf = max(0.44, conf - 0.02)
        score = max(38.0, score - 1.5)
    return conf, score
'''
    OPTIMIZER_PATH.write_text(optimizer_code, encoding="utf-8")
    # Hot-reload: live bot picks this up via live_update without restart.

    record = {
        "applied_at": _utc(),
        "source": source,
        "params": row,
        "oos_score": oos_score,
        "oos_avg_return_pct": oos_avg_return,
        "conf_relax": conf_relax,
        "score_relax": score_relax,
    }
    APPLIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPLIED_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    log.info("applied god-bot tuning %s oos=%s", row, oos_score)
    return record


def apply_from_fold_report(fold_report: dict[str, Any]) -> dict[str, Any]:
    params = fold_report.get("best_params") or {}
    return apply_params_to_god_bot(
        params,
        source="walk_forward_fold",
        oos_score=fold_report.get("oos_score"),
        oos_avg_return=fold_report.get("oos_avg_return_pct"),
    )


def apply_from_full_report(report: dict[str, Any]) -> dict[str, Any]:
    best = report.get("best_fold") or {}
    params = best.get("best_params") or report.get("recommended_params") or {}
    return apply_params_to_god_bot(
        params,
        source="walk_forward_final",
        oos_score=best.get("oos_score"),
        oos_avg_return=best.get("oos_avg_return_pct"),
    )
