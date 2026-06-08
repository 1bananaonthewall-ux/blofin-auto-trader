#!/usr/bin/env python3
"""
Run walk-forward backtest rounds; watch live_progress.json and log tuning.

Usage:
  python scripts/god_backtest_live_optimize.py --rounds 3 --max-assets 120
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("god_backtest_live")

PROGRESS = ROOT / "state" / "god_backtest" / "live_progress.json"


def _read_progress() -> dict:
    if not PROGRESS.is_file():
        return {}
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--lookback-days", type=int, default=365)
    p.add_argument("--max-assets", type=int, default=120, help="0=full universe")
    p.add_argument("--train-days", type=int, default=90)
    p.add_argument("--test-days", type=int, default=21)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--refit-ml", action="store_true")
    p.add_argument("--no-live-safe", action="store_true", help="Allow more workers; still never stops bot")
    p.add_argument("--no-ws", action="store_true", help="Skip WS tail (safer when live bot uses same API)")
    args = p.parse_args()

    from god_backtest.trade_safe import ensure_live_bot_healthy, live_health_snapshot

    health = ensure_live_bot_healthy(restart_if_stale=True)
    log.info("live bot health before backtest: %s", health)
    if not health.get("bot_running"):
        log.warning("live bot not detected — backtest will still avoid stack stop/restart")

    from god_backtest.engine import run_walkforward_backtest

    asset_steps = [args.max_assets]
    if args.rounds >= 2 and args.max_assets > 0:
        asset_steps.append(min(493, args.max_assets * 2))
    if args.rounds >= 3:
        asset_steps.append(0)

    for rnd, max_assets in enumerate(asset_steps[: args.rounds], start=1):
        log.info("=== round %d/%d | max_assets=%s ===", rnd, args.rounds, max_assets or "ALL")
        try:
            report = run_walkforward_backtest(
                lookback_days=args.lookback_days,
                max_assets=max_assets,
                train_days=args.train_days,
                test_days=args.test_days,
                max_workers=args.workers,
                refit_ml=args.refit_ml,
                use_cache=True,
                live_safe=not args.no_live_safe,
                use_ws_tail=not args.no_ws,
            )
        except Exception as exc:
            log.exception("round %d crashed: %s", rnd, exc)
            if rnd < args.rounds:
                log.info("cooldown 120s before retry next round")
                time.sleep(120)
                continue
            return 1
        log.info("live bot after round: %s", live_health_snapshot())
        log.info(
            "round %d done | ok=%s avg_oos=%.4f params=%s",
            rnd,
            report.get("ok"),
            report.get("avg_oos_score") or 0,
            report.get("recommended_params"),
        )
        if not report.get("ok"):
            log.error("round %d failed: %s", rnd, report.get("error"))
            if rnd < args.rounds:
                log.info("cooldown 120s before next round (API rate limit recovery)")
                time.sleep(120)
                continue
            return 1
        if rnd < args.rounds:
            log.info("round %d cooldown 90s (protect live bot + API)", rnd)
            time.sleep(90)

    log.info("all rounds complete — see state/god_backtest/walkforward_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
