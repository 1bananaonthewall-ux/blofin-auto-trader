#!/usr/bin/env python3
"""
Project hook: remind Cursor agent when 5-minute God Bot check is due (local IDE).

Runs on sessionStart. Creates .cursor/AGENT_5M_DUE when last pass is stale.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state" / "last_cursor_5m.txt"
FLAG = ROOT / ".cursor" / "AGENT_5M_DUE"
HOURLY = ROOT / ".cursor" / "HOURLY_DUE"
INTERVAL_SEC = 300


def main() -> None:
    now = time.time()
    last = 0.0
    if STATE.is_file():
        try:
            last = float(STATE.read_text(encoding="utf-8").strip())
        except ValueError:
            last = 0.0

    due = (now - last) >= INTERVAL_SEC
    if due and not HOURLY.is_file():
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.write_text(
            f"due_since={int(now)}\ninterval_sec={INTERVAL_SEC}\n",
            encoding="utf-8",
        )
        msg = (
            "AGENT_5M_DUE: God Bot 5-minute maintain is due. "
            "Run blofin-5m skill (.cursor/skills/blofin-5m/SKILL.md): "
            "vertical curve, throughput, ML health, stack ensure, brief fix pass."
        )
        print(json.dumps({"continue": True, "agent_message": msg}))
    else:
        if FLAG.is_file() and not due:
            FLAG.unlink(missing_ok=True)
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
