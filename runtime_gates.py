"""Runtime entry gates written by log_watch_optimizer / stack guard (no bot restart)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def pause_path(state_dir: Path) -> Path:
    return state_dir / "entries_pause.json"


def read_entries_pause(state_dir: Path) -> tuple[bool, str]:
    path = pause_path(state_dir)
    if not path.is_file():
        return False, ""
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, ""
    until = float(raw.get("until_ts") or 0)
    if until <= time.time():
        return False, ""
    reason = str(raw.get("reason") or "log watch pause")
    return True, reason


def set_entries_pause(state_dir: Path, seconds: float, reason: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    pause_path(state_dir).write_text(
        json.dumps(
            {
                "until_ts": time.time() + max(30.0, seconds),
                "reason": reason,
                "set_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_entries_pause(state_dir: Path) -> None:
    path = pause_path(state_dir)
    if path.is_file():
        path.unlink(missing_ok=True)
