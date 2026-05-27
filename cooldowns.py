from __future__ import annotations

import json
import time
from pathlib import Path


class SymbolCooldowns:
    """Adaptive cooldowns based on trade outcomes.
    
    After a loss: longer cooldown (multiplicative).
    After a win: normal cooldown or skip.
    Consecutive losses: exponentially increasing cooldown.
    """
    def __init__(self, path: Path, cooldown_seconds: int) -> None:
        self.path = path
        self.cooldown_seconds = cooldown_seconds
        self._data: dict[str, float] = {}
        self._consecutive_losses: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw.get("cooldowns", raw) if isinstance(raw, dict) else raw
                self._consecutive_losses = raw.get("losses", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"cooldowns": self._data, "losses": self._consecutive_losses}, indent=2),
            encoding="utf-8",
        )

    def is_blocked(self, symbol: str) -> bool:
        until = self._data.get(symbol, 0)
        return until > time.time()

    def block(self, symbol: str, seconds: int | None = None) -> None:
        """Block a symbol for the given seconds (default: cooldown_seconds)."""
        duration = seconds if seconds is not None else self.cooldown_seconds
        self._data[symbol] = time.time() + duration
        self._save()

    def mark_loss(self, symbol: str) -> None:
        """After a losing trade: exponential cooldown."""
        consec = self._consecutive_losses.get(symbol, 0) + 1
        self._consecutive_losses[symbol] = consec
        # Exponential backoff: 1x, 2x, 4x, 8x base cooldown
        multiplier = 2 ** (consec - 1) if consec > 0 else 1
        duration = self.cooldown_seconds * multiplier
        if duration > 0:
            self.block(symbol, seconds=duration)

    def mark_win(self, symbol: str) -> None:
        """After a winning trade: reset loss counter, apply normal cooldown."""
        self._consecutive_losses.pop(symbol, None)
        if self.cooldown_seconds > 0:
            self.block(symbol, seconds=self.cooldown_seconds)

    def reset(self, symbol: str) -> None:
        """Remove cooldown for a symbol."""
        self._data.pop(symbol, None)
        self._consecutive_losses.pop(symbol, None)
        self._save()