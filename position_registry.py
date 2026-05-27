"""Track when positions opened for maturity / rotation logic."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class PositionRegistry:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "position_registry.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def record_open(
        self,
        symbol: str,
        *,
        side: str,
        entry_price: float,
        leverage: int,
        stop_pct: float,
        take_pct: float,
        conviction: float,
    ) -> None:
        self._data[symbol] = {
            "opened_at": time.time(),
            "side": side,
            "entry_price": entry_price,
            "leverage": leverage,
            "stop_pct": stop_pct,
            "take_pct": take_pct,
            "conviction": conviction,
        }
        self._save()

    def update_tpsl(self, symbol: str, *, stop_pct: float, take_pct: float) -> None:
        row = self._data.get(symbol)
        if not row:
            return
        row["stop_pct"] = stop_pct
        row["take_pct"] = take_pct
        self._save()

    def update_leverage(self, symbol: str, *, leverage: int) -> None:
        row = self._data.get(symbol)
        if not row:
            return
        row["leverage"] = leverage
        self._save()

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self._data.get(symbol)

    def remove(self, symbol: str) -> None:
        self._data.pop(symbol, None)
        self._save()

    def sync_with_exchange(self, open_symbols: set[str]) -> None:
        stale = [s for s in self._data if s not in open_symbols]
        for s in stale:
            self._data.pop(s, None)
        if stale:
            self._save()
