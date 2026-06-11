#!/usr/bin/env python3
"""
God Bot caretaker — health probe + safe auto-restart (no Cursor required).

Writes state/caretaker_tick.json. Sets .cursor/GODBOT_CARETAKER_DUE when the
agent should fix code / investigate crash loops beyond a simple restart.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _python() -> str:
    if _VENV_PY.is_file():
        return str(_VENV_PY)
    return sys.executable

STATE_PATH = ROOT / "state" / "caretaker_tick.json"
FLAG_PATH = ROOT / ".cursor" / "GODBOT_CARETAKER_DUE"
LOG_PATH = ROOT / "logs" / "caretaker.log"
STACK_PS1 = ROOT / "scripts" / "stack_control.ps1"

RESTART_COOLDOWN_SEC = 300
MAX_RESTARTS_PER_HOUR = 8
STALE_LOG_SEC = 180
CRASH_RES = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ImportError:"),
    re.compile(r"SyntaxError:"),
    re.compile(r"IndentationError:"),
]


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def _load_prev() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stack_ready() -> dict:
    try:
        out = subprocess.check_output(
            [_python(), str(ROOT / "scripts" / "stack_repair_check.py")],
            text=True,
            timeout=25,
            cwd=str(ROOT),
        )
        return json.loads(out.strip())
    except Exception as exc:
        return {"ready": False, "bot_running": False, "dashboard_listening": False, "error": str(exc)[:120]}


def _count_bot_processes() -> int:
    """Count distinct bot PIDs from stack_control (ignores parent/child pairs)."""
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STACK_PS1),
                "-Action",
                "status",
            ],
            text=True,
            timeout=25,
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
        )
        pids = {int(m) for m in re.findall(r"RUNNING pid=(\d+)", out)}
        return len(pids)
    except Exception:
        return 0


def _tail_bot_log(n: int = 80) -> list[str]:
    path = ROOT / "logs" / "bot.log"
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _crash_in_tail(lines: list[str]) -> str:
    for line in reversed(lines):
        for pat in CRASH_RES:
            if pat.search(line):
                return line.strip()[:200]
    return ""


def _recent_restarts(prev: dict, now: float) -> list[float]:
    hist = [float(x) for x in (prev.get("restart_history") or []) if x]
    return [t for t in hist if now - t < 3600]


def _run_stack(action: str) -> str:
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STACK_PS1),
                "-Action",
                action,
            ],
            text=True,
            timeout=240,
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
        )
        return (out or "")[-800:]
    except subprocess.CalledProcessError as exc:
        return (exc.output or str(exc))[-800:]
    except Exception as exc:
        return str(exc)[:400]


def tick(*, auto_repair: bool = True) -> dict:
    from god_backtest.trade_safe import bot_log_age_sec, bot_log_fresh, live_bot_running

    now = time.time()
    prev = _load_prev()
    ready = _stack_ready()
    bot_running = bool(ready.get("bot_running"))
    dash_ok = bool(ready.get("dashboard_listening"))
    log_age = bot_log_age_sec()
    log_fresh = bot_log_fresh(max_age_sec=STALE_LOG_SEC)
    dup_bots = _count_bot_processes()
    tail = _tail_bot_log()
    crash_line = _crash_in_tail(tail)
    restart_hist = _recent_restarts(prev, now)

    anomalies: list[str] = []
    agent_reasons: list[str] = []
    actions_taken: list[str] = []
    planned = "ok"

    if not bot_running:
        anomalies.append("bot_down")
        planned = "ensure"
    if not dash_ok:
        anomalies.append("dashboard_down")
        if planned == "ok":
            planned = "ensure"
    if bot_running and not log_fresh:
        anomalies.append(f"bot_log_stale age={log_age:.0f}s" if log_age is not None else "bot_log_missing")
        planned = "restart-fresh" if planned == "ok" else planned
    if dup_bots > 1:
        anomalies.append(f"duplicate_bots={dup_bots}")
        planned = "restart-fresh"
    if crash_line:
        anomalies.append("crash_in_log")
        agent_reasons.append(crash_line)
        if planned in ("ok", "ensure"):
            planned = "restart-fresh"

    last_restart = float(prev.get("last_restart_ts") or 0)
    cooldown_ok = (now - last_restart) >= RESTART_COOLDOWN_SEC
    restarts_ok = len(restart_hist) < MAX_RESTARTS_PER_HOUR

    action = "ok"
    if planned != "ok" and auto_repair:
        if cooldown_ok and restarts_ok:
            action = planned
            _log(f"auto {action} anomalies={anomalies}")
            stack_out = _run_stack(action)
            actions_taken.append(f"stack:{action}")
            actions_taken.append(f"stack_out:{stack_out[-200:].replace(chr(10), ' ')}")
            restart_hist.append(now)
            time.sleep(12)
            ready = _stack_ready()
            bot_running = bool(ready.get("bot_running"))
            dash_ok = bool(ready.get("dashboard_listening"))
            log_fresh = bot_log_fresh(max_age_sec=STALE_LOG_SEC)
            if not ready.get("ready"):
                agent_reasons.append(
                    f"stack still unhealthy after {action}: bot={bot_running} dash={dash_ok}"
                )
        else:
            action = "cooldown"
            agent_reasons.append(
                f"restart suppressed cooldown={not cooldown_ok} hourly_cap={not restarts_ok}"
            )

    # Run lightweight maintainers (same as stack_guard, idempotent).
    if auto_repair and bot_running:
        for script in (
            "log_watch_optimizer.py",
            "repair_open_tpsl.py",
            "curve_guard_daemon.py",
        ):
            path = ROOT / "scripts" / script
            if not path.is_file():
                continue
            try:
                args = [_python(), str(path)]
                if script == "curve_guard_daemon.py":
                    args.append("--once")
                subprocess.run(args, cwd=str(ROOT), timeout=120, check=False)
                actions_taken.append(f"ran:{script}")
            except Exception as exc:
                anomalies.append(f"{script}_fail:{exc}")

    if agent_reasons or (planned != "ok" and action in ("cooldown", "ok")):
        FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        FLAG_PATH.write_text(
            "\n".join(
                [
                    f"due_since={int(now)}",
                    f"planned={planned}",
                    f"action={action}",
                    f"anomalies={','.join(anomalies)}",
                    f"agent_reasons={'; '.join(agent_reasons)[:500]}",
                    "instruction=Run god-bot-caretaker skill: diagnose, fix code if needed, restart stack until healthy.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    elif FLAG_PATH.is_file() and ready.get("ready") and log_fresh and dup_bots <= 1:
        FLAG_PATH.unlink(missing_ok=True)

    report = {
        "ts": now,
        "ready": bool(ready.get("ready")),
        "bot_running": bot_running,
        "dashboard_listening": dash_ok,
        "bot_log_age_sec": round(log_age, 1) if log_age is not None else None,
        "bot_log_fresh": log_fresh,
        "duplicate_bots": dup_bots,
        "planned": planned,
        "action": action,
        "actions_taken": actions_taken,
        "anomalies": anomalies,
        "agent_reasons": agent_reasons,
        "restart_history": restart_hist[-MAX_RESTARTS_PER_HOUR:],
        "last_restart_ts": max(restart_hist) if restart_hist else last_restart,
        "agent_due": FLAG_PATH.is_file(),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return report


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="God Bot caretaker health tick")
    p.add_argument("--no-repair", action="store_true", help="Probe only; do not restart stack")
    args = p.parse_args()
    rep = tick(auto_repair=not args.no_repair)
    return 0 if rep.get("ready") and not rep.get("agent_reasons") else 1


if __name__ == "__main__":
    raise SystemExit(main())
