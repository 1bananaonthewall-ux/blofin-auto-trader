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
        margin_usdt: float | None = None,
        contracts: float | None = None,
        trade_style: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "opened_at": time.time(),
            "side": side,
            "entry_price": entry_price,
            "leverage": leverage,
            "stop_pct": stop_pct,
            "take_pct": take_pct,
            "conviction": conviction,
        }
        if trade_style:
            row["trade_style"] = str(trade_style)
        if margin_usdt is not None and margin_usdt > 0:
            row["margin_usdt"] = round(float(margin_usdt), 6)
        if contracts is not None and contracts > 0:
            row["contracts"] = float(contracts)
        self._data[symbol] = row
        self._save()

    def update_tpsl(
        self,
        symbol: str,
        *,
        stop_pct: float,
        take_pct: float,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
    ) -> None:
        row = self._data.get(symbol)
        if not row:
            return
        row["stop_pct"] = stop_pct
        row["take_pct"] = take_pct
        if sl_price > 0:
            row["sl_price"] = sl_price
        if tp_price > 0:
            row["tp_price"] = tp_price
        row["tpsl_verified_at"] = time.time()
        self._save()

    def update_leverage(self, symbol: str, *, leverage: int) -> None:
        row = self._data.get(symbol)
        if not row:
            return
        row["leverage"] = leverage
        self._save()

    def get(self, symbol: str) -> dict[str, Any] | None:
        if symbol in self._data:
            return self._data[symbol]
        base = str(symbol).split("#")[0]
        return self._data.get(base) if base != symbol else None

    def remove(self, symbol: str) -> None:
        self._data.pop(symbol, None)
        self._save()

    def stale_symbols(self, open_symbols: set[str]) -> list[str]:
        return [s for s in self._data if s not in open_symbols]

    def pop_meta(self, symbol: str) -> dict[str, Any] | None:
        row = self._data.pop(symbol, None)
        if row is not None:
            self._save()
        return row

    def sync_with_exchange(self, open_symbols: set[str], *, api_ok: bool = True) -> None:
        if not api_ok:
            return
        stale = [s for s in self._data if s not in open_symbols]
        for s in stale:
            self._data.pop(s, None)
        if stale:
            self._save()
