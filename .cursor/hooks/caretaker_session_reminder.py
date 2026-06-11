#!/usr/bin/env python3
"""
Session hook: nudge Cursor agent when God Bot caretaker check is due.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state" / "last_cursor_caretaker.txt"
FLAG = ROOT / ".cursor" / "GODBOT_CARETAKER_DUE"
INTERVAL_SEC = 900  # 15 minutes


def main() -> None:
    now = time.time()
    last = 0.0
    if STATE.is_file():
        try:
            last = float(STATE.read_text(encoding="utf-8").strip())
        except ValueError:
            last = 0.0

    tick_path = ROOT / "state" / "caretaker_tick.json"
    agent_needed = False
    if tick_path.is_file():
        try:
            tick = json.loads(tick_path.read_text(encoding="utf-8"))
            agent_needed = bool(tick.get("agent_due") or tick.get("agent_reasons"))
        except Exception:
            pass

    due = agent_needed or (now - last) >= INTERVAL_SEC
    if due:
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        if not FLAG.is_file():
            FLAG.write_text(
                f"due_since={int(now)}\ninterval_sec={INTERVAL_SEC}\nsource=session_hook\n",
                encoding="utf-8",
            )
        msg = (
            "GODBOT_CARETAKER_DUE: Run god-bot-caretaker skill "
            "(.cursor/skills/god-bot-caretaker/SKILL.md): health check, "
            "auto-restart stack if needed, fix log/code blockers."
        )
        print(json.dumps({"continue": True, "agent_message": msg}))
    else:
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
