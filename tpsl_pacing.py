"""
Entry pacing driven only by exchange-style TP/SL outcomes.

Scanning continues every tick; global open gap applies only after a classified TP or SL exit.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings


def use_tpsl_only_pacing(settings: "Settings") -> bool:
    if bool(getattr(settings, "tpsl_only_pacing", False)):
        return True
    from hourly_3r import hourly_3r_active

    return hourly_3r_active(settings)


def classify_exit_event(event: str, net_pnl_usd: float = 0.0) -> str | None:
    """Return 'tp', 'sl', or None (no pacing effect)."""
    e = (event or "").lower()
    if any(k in e for k in ("tp_backup", "tp_hit", "take_profit", "tp_breach", "tp ")):
        return "tp"
    if any(k in e for k in ("sl_backup", "sl_hit", "stop_loss", "sl_breach", "sl ")):
        return "sl"
    if "harvest" in e:
        if net_pnl_usd > 0:
            return "tp"
        if net_pnl_usd < 0:
            return "sl"
        return None
    if e in {"tp", "take"}:
        return "tp"
    if e in {"sl", "stop"}:
        return "sl"
    return None


class TpslPacer:
    """Global gap between new opens — set only by last TP/SL-classified exit."""

    def __init__(self, state_dir: Path, settings: "Settings") -> None:
        self.path = state_dir / "tpsl_pacer.json"
        self.base_gap = float(getattr(settings, "tpsl_pace_base_gap_seconds", 2.0))
        self.gap_after_tp = float(getattr(settings, "tpsl_pace_gap_after_tp_seconds", 4.0))
        self.gap_after_sl = float(getattr(settings, "tpsl_pace_gap_after_sl_seconds", 12.0))
        self.symbol_sl_cooldown = float(
            getattr(settings, "tpsl_pace_symbol_sl_cooldown_seconds", 25.0)
        )
        self._last_exit_ts = 0.0
        self._pending_gap = 0.0
        self._last_kind = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._last_exit_ts = float(raw.get("last_exit_ts", 0))
            self._pending_gap = float(raw.get("pending_gap", 0))
            self._last_kind = str(raw.get("last_kind", ""))
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "last_exit_ts": self._last_exit_ts,
                    "pending_gap": self._pending_gap,
                    "last_kind": self._last_kind,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def record_exit(self, event: str, net_pnl_usd: float = 0.0) -> str | None:
        kind = classify_exit_event(event, net_pnl_usd)
        if kind is None:
            return None
        self._last_kind = kind
        self._pending_gap = self.gap_after_tp if kind == "tp" else self.gap_after_sl
        self._last_exit_ts = time.time()
        self._save()
        return kind

    def seconds_until_ready(self) -> float:
        if self._last_exit_ts <= 0:
            return 0.0
        elapsed = time.time() - self._last_exit_ts
        return max(0.0, self._pending_gap - elapsed)

    def can_open(self) -> bool:
        return self.seconds_until_ready() <= 0

    def symbol_cooldown_seconds(self, kind: str | None) -> float:
        if kind == "sl":
            return self.symbol_sl_cooldown
        return 0.0
