#!/usr/bin/env python3
"""
Project hook: remind Cursor agent when hourly Blofin check is due (local IDE).

Runs on sessionStart. Creates .cursor/HOURLY_DUE for rules to pick up.
Does not run trades — the agent runs blofin-hourly skill.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state" / "last_cursor_hourly.txt"
FLAG = ROOT / ".cursor" / "HOURLY_DUE"
INTERVAL_SEC = 3600


def main() -> None:
    now = time.time()
    last = 0.0
    if STATE.is_file():
        try:
            last = float(STATE.read_text(encoding="utf-8").strip())
        except ValueError:
            last = 0.0

    due = (now - last) >= INTERVAL_SEC
    if due:
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.write_text(
            f"due_since={int(now)}\ninterval_sec={INTERVAL_SEC}\n",
            encoding="utf-8",
        )
        msg = (
            "HOURLY_MAINTENANCE_DUE: Blofin hourly check is due. "
            "Before other work, run the blofin-hourly skill "
            "(.cursor/skills/blofin-hourly/SKILL.md): health report, "
            "close non-50x positions, optimizer pass, log summary."
        )
        print(json.dumps({"continue": True, "agent_message": msg}))
    else:
        if FLAG.is_file():
            FLAG.unlink(missing_ok=True)
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
