#!/usr/bin/env python3
"""Background curve monitor — repairs equity_ticks when chart drifts from live balance."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from curve_guard import run_guard_tick

DEFAULT_INTERVAL_SEC = 60.0
PID_NAME = "curve_guard.pid"


def _state_dir() -> Path:
    return ROOT / "state"


def _log_path() -> Path:
    return ROOT / "logs" / "curve_guard.log"


def _pid_path() -> Path:
    return _state_dir() / PID_NAME


def _write_pid() -> None:
    path = _pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="ascii")


def _clear_pid() -> None:
    _pid_path().unlink(missing_ok=True)


def tick_once(*, force: bool = False) -> int:
    status = run_guard_tick(_state_dir(), force_repair=force, log_path=_log_path())
    if status.get("repaired"):
        print(
            f"curve_guard repaired anchor={status.get('metrics', {}).get('anchor')} "
            f"issues={status.get('issues')}"
        )
    elif status.get("healthy"):
        print("curve_guard ok")
    else:
        print(f"curve_guard unhealthy issues={status.get('issues')}")
    return 0 if status.get("healthy") else 1


def daemon_loop(interval_sec: float) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    _write_pid()
    print(f"curve_guard daemon pid={os.getpid()} interval={interval_sec}s")
    try:
        while True:
            try:
                run_guard_tick(_state_dir(), log_path=_log_path())
            except Exception as exc:
                logging.exception("curve_guard tick failed: %s", exc)
            time.sleep(max(15.0, interval_sec))
    except KeyboardInterrupt:
        return 0
    finally:
        _clear_pid()


def main() -> int:
    parser = argparse.ArgumentParser(description="Equity curve guard")
    parser.add_argument("--once", action="store_true", help="Single check/repair pass")
    parser.add_argument("--force", action="store_true", help="Force repair even if healthy")
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("CURVE_GUARD_INTERVAL_SEC", DEFAULT_INTERVAL_SEC)),
        help="Daemon loop interval seconds",
    )
    args = parser.parse_args()
    if args.once:
        return tick_once(force=args.force)
    return daemon_loop(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
