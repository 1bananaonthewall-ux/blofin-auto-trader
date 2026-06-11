"""God Bot full-universe walk-forward backtest orchestrator."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from god_backtest.candle_cache import prefetch_universe
from god_backtest.trade_safe import (
    ensure_live_bot_healthy,
    live_health_snapshot,
    recommend_workers,
    set_below_normal_priority,
)
from god_backtest.ml_refit import refit_forward_model
from god_backtest.period import fold_windows, resolve_god_backtest_range
from god_backtest.walk_forward import run_walk_forward_fold
from god_backtest.ws_tail import sync_ws_tails
from storefront_market import list_tradeable_assets

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "state" / "god_backtest"
LIVE_PROGRESS = REPORT_DIR / "live_progress.json"


def _write_live_progress(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = time.time()
    LIVE_PROGRESS.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_walkforward_backtest(
    *,
    starting_pot: float = 1000.0,
    lookback_days: int | None = 3650,
    start_date: str | None = None,
    end_date: str | None = None,
    max_assets: int = 0,
    train_days: int = 120,
    test_days: int = 30,
    step_days: int | None = None,
    max_workers: int = 8,
    use_ws_tail: bool = True,
    refit_ml: bool = False,
    use_cache: bool = True,
    live_safe: bool = True,
    apply_live: bool = False,
) -> dict[str, Any]:
    """
    Walk-forward God Bot backtest on Blofin USDT perp universe.

    Notes:
    - Requests up to 10y history; Blofin returns whatever exists per listing (often months–2y).
    - WebSocket tail sync freshens recent bars; bulk history is parallel REST + gzip cache.
    - Optimizes gate params on train windows, scores out-of-sample test windows.
    - apply_live=False by default: backtest reports only; never mutates live gates/ML.
    """
    t0 = time.time()
    if live_safe:
        set_below_normal_priority()
        live_health = ensure_live_bot_healthy(restart_if_stale=False)
        max_workers = recommend_workers(max_workers, live_safe=True)
        log.info("live-safe backtest | workers=%d | live=%s", max_workers, live_health)
    else:
        live_health = live_health_snapshot()

    period = resolve_god_backtest_range(
        start_date=start_date,
        end_date=end_date,
        lookback_days=lookback_days,
    )
    all_assets = list_tradeable_assets()
    assets = all_assets if max_assets <= 0 else all_assets[:max_assets]
    inst_ids = [a["inst_id"] for a in assets]

    log.info(
        "God backtest | %d assets | %s → %s | train=%dd test=%dd",
        len(assets),
        period["start_date"],
        period["end_date"],
        train_days,
        test_days,
    )

    ws_tails: dict[str, dict[str, list[list[float]]]] = {}
    ws_thread: threading.Thread | None = None
    _write_live_progress(
        {
            "phase": "prefetch",
            "assets": len(assets),
            "period": period,
            "folds_planned": 0,
            "folds_done": 0,
            "live_bot": live_health,
            "live_safe": live_safe,
            "max_workers": max_workers,
        }
    )

    if use_ws_tail and inst_ids:
        def _ws_worker() -> None:
            try:
                ws_tails.update(sync_ws_tails(inst_ids[:80], timeout_sec=12.0))
            except Exception as exc:
                log.warning("ws tail sync skipped: %s", exc)

        ws_thread = threading.Thread(target=_ws_worker, daemon=True)
        ws_thread.start()

    prefetch = prefetch_universe(
        inst_ids,
        start_ms=period["start_ms"],
        end_ms=period["end_ms"],
        max_workers=max_workers,
        use_cache=use_cache,
    )
    if ws_thread:
        ws_thread.join(timeout=25.0)

    folds = fold_windows(
        period["start_ms"],
        period["end_ms"],
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
    )
    if not folds:
        return {
            "ok": False,
            "error": "insufficient_history_for_folds",
            "period": period,
            "prefetch": prefetch,
            "hint": "Reduce train_days/test_days or shorten lookback — not enough bars returned.",
        }

    fold_reports: list[dict[str, Any]] = []
    ml_reports: list[dict[str, Any]] = []

    _write_live_progress(
        {
            "phase": "walk_forward",
            "assets": len(assets),
            "prefetch": prefetch,
            "folds_planned": len(folds),
            "folds_done": 0,
            "fold_reports": [],
            "avg_oos_score": None,
        }
    )

    from god_backtest.apply_tuning import apply_from_fold_report

    for i, fold in enumerate(folds):
        log.info("walk-forward fold %d/%d", i + 1, len(folds))
        if refit_ml:
            try:
                from config import load_settings

                ml_reports.append(
                    refit_forward_model(
                        assets=assets,
                        train_start_ms=fold["train_start_ms"],
                        train_end_ms=fold["train_end_ms"],
                        settings=load_settings(),
                    )
                )
            except Exception as exc:
                ml_reports.append({"ok": False, "error": str(exc)[:200]})

        fold_result = run_walk_forward_fold(
            fold,
            assets,
            starting_pot=starting_pot,
            max_workers=max_workers,
            ws_tails=ws_tails,
        )
        fold_reports.append(fold_result)

        oos_so_far = [f["oos_score"] for f in fold_reports]
        applied = None
        if (
            apply_live
            and fold_result.get("oos_score", -1e9) > 0
            and fold_result.get("oos_total_trades", 0) >= 10
        ):
            try:
                applied = apply_from_fold_report(fold_result)
            except Exception as exc:
                log.warning("live apply tuning failed: %s", exc)

        _write_live_progress(
            {
                "phase": "walk_forward",
                "assets": len(assets),
                "prefetch": prefetch,
                "folds_planned": len(folds),
                "folds_done": i + 1,
                "latest_fold": fold_result,
                "fold_reports": fold_reports,
                "avg_oos_score": round(sum(oos_so_far) / len(oos_so_far), 4) if oos_so_far else None,
                "last_applied_tuning": applied,
                "live_bot": live_health_snapshot(),
            }
        )

    oos_scores = [f["oos_score"] for f in fold_reports]
    best_fold = max(fold_reports, key=lambda f: f["oos_score"]) if fold_reports else None
    avg_oos = sum(oos_scores) / len(oos_scores) if oos_scores else 0.0

    report = {
        "ok": True,
        "engine": "god_walkforward_v1",
        "brand": "God Bot",
        "spec_base": "god-bot",
        "period": period,
        "starting_pot": starting_pot,
        "universe_total": len(all_assets),
        "assets_tested": len(assets),
        "prefetch": prefetch,
        "ws_tail_symbols": len(ws_tails),
        "folds": len(fold_reports),
        "train_days": train_days,
        "test_days": test_days,
        "avg_oos_score": round(avg_oos, 4),
        "best_fold": best_fold,
        "fold_reports": fold_reports,
        "ml_refit": ml_reports if refit_ml else None,
        "recommended_params": best_fold.get("best_params") if best_fold else None,
        "elapsed_sec": round(time.time() - t0, 1),
        "disclaimer": (
            "Simulated walk-forward on Blofin OHLCV. Blofin perp history is limited per symbol; "
            "10y is requested but actual depth varies. Not financial advice."
        ),
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / "walkforward_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("report saved %s | avg_oos=%.4f", out_path, avg_oos)

    from god_backtest.apply_tuning import apply_from_full_report

    if apply_live:
        try:
            report["applied_tuning"] = apply_from_full_report(report)
        except Exception as exc:
            log.warning("final apply tuning failed: %s", exc)
    else:
        report["applied_tuning"] = {"live_apply": False, "note": "report-only; live bot unchanged"}
        log.info("walk-forward complete — live apply skipped (apply_live=False)")

    _write_live_progress(
        {
            "phase": "done",
            "assets": len(assets),
            "folds_planned": len(fold_reports),
            "folds_done": len(fold_reports),
            "avg_oos_score": report["avg_oos_score"],
            "recommended_params": report.get("recommended_params"),
            "applied_tuning": report.get("applied_tuning"),
            "report_path": str(out_path),
        }
    )
    return report
