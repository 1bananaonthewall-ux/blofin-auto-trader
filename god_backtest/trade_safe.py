"""Keep live God Bot trading while backtests run — never touch the stack."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
BOT_LOG = ROOT / "logs" / "bot.log"


def live_bot_running() -> bool:
    try:
        from whatsapp_agent import is_bot_running

        return is_bot_running()
    except Exception:
        return False


def bot_log_age_sec() -> float | None:
    if not BOT_LOG.is_file():
        return None
    return max(0.0, time.time() - BOT_LOG.stat().st_mtime)


def bot_log_fresh(max_age_sec: float = 120.0) -> bool:
    age = bot_log_age_sec()
    return age is not None and age <= max_age_sec


def set_below_normal_priority() -> None:
    """Yield CPU to live bot.py on Windows."""
    if sys.platform != "win32":
        return
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return
    except Exception:
        pass
    try:
        import ctypes

        BELOW_NORMAL = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    except Exception:
        log.debug("could not set below-normal priority")


def recommend_workers(requested: int, *, live_safe: bool) -> int:
    if not live_safe:
        return requested
    if live_bot_running():
        return max(2, min(requested, 4))
    return requested


def live_health_snapshot() -> dict[str, Any]:
    age = bot_log_age_sec()
    return {
        "bot_running": live_bot_running(),
        "bot_log_exists": BOT_LOG.is_file(),
        "bot_log_age_sec": round(age, 1) if age is not None else None,
        "bot_log_fresh": bot_log_fresh(),
    }


def ensure_live_bot_healthy(*, restart_if_stale: bool = False) -> dict[str, Any]:
    """
    Never stop the bot for backtest. Optionally ensure if down or logs frozen.
    """
    snap = live_health_snapshot()
    if snap["bot_running"] and snap.get("bot_log_fresh"):
        return {**snap, "action": "ok"}

    if not restart_if_stale:
        return {**snap, "action": "warn_stale" if snap["bot_running"] else "warn_down"}

    ps1 = ROOT / "scripts" / "stack_control.ps1"
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Action", "ensure"],
            cwd=str(ROOT),
            timeout=200,
            check=False,
        )
        snap = live_health_snapshot()
        return {**snap, "action": "ensure_called"}
    except Exception as exc:
        return {**snap, "action": "ensure_failed", "error": str(exc)[:200]}
