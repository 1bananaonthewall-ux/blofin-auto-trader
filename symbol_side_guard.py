"""Block long→short (or short→long) flip-flops on the same symbol within a window."""

from __future__ import annotations

import json
import time
from pathlib import Path


class SymbolSideGuard:
    def __init__(self, state_dir: Path, block_seconds: float = 1200.0) -> None:
        self.path = state_dir / "symbol_side_guard.json"
        self.block_seconds = max(60.0, block_seconds)
        self._last: dict[str, dict[str, float | str]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._last = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._last = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._last, indent=2), encoding="utf-8")

    def is_flip_blocked(self, symbol: str, side: str) -> tuple[bool, str]:
        row = self._last.get(symbol)
        if not row:
            return False, ""
        last_side = str(row.get("side", "")).lower()
        ts = float(row.get("ts", 0))
        if not last_side or last_side == side.lower():
            return False, ""
        elapsed = time.time() - ts
        if elapsed < self.block_seconds:
            remain = int(self.block_seconds - elapsed)
            return True, f"flip {last_side}→{side} blocked ({remain}s left)"
        return False, ""

    def record(self, symbol: str, side: str) -> None:
        self._last[symbol] = {"side": side.lower(), "ts": time.time()}
        self._save()
