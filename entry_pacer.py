"""Pace entries — scan many symbols, act on one best conviction at a time."""

from __future__ import annotations

import json
import time
from pathlib import Path


class EntryPacer:
    """Global minimum gap between new positions (any symbol)."""

    def __init__(self, state_dir: Path, min_seconds: float = 75.0) -> None:
        self.path = state_dir / "entry_pacer.json"
        self.min_seconds = min_seconds
        self._last_entry_ts = 0.0
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._last_entry_ts = float(raw.get("last_entry_ts", 0))
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"last_entry_ts": self._last_entry_ts}, indent=2),
            encoding="utf-8",
        )

    def set_min_seconds(self, seconds: float, *, floor: float = 30.0) -> None:
        self.min_seconds = max(floor, seconds)

    def seconds_until_ready(self) -> float:
        elapsed = time.time() - self._last_entry_ts
        return max(0.0, self.min_seconds - elapsed)

    def can_enter(self) -> bool:
        return self.seconds_until_ready() <= 0

    def record_entry(self) -> None:
        self._last_entry_ts = time.time()
        self._save()
