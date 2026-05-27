"""
Account composure controller — chill on plummet, ramp heat slowly on recovery.

Modes:
  NORMAL     — full autonomous knobs
  CAUTION    — tighter gates, half risk (early drawdown)
  CHILL      — no new entries; maintain SL/TP; force ML learn from losses
  RECOVERING — gradual heat ramp back toward NORMAL
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class AccountMode(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    CHILL = "chill"
    RECOVERING = "recovering"


@dataclass
class GuardSnapshot:
    mode: AccountMode
    heat_factor: float  # 0.0 = ice cold, 1.0 = full heat
    allow_new_entries: bool
    drawdown_pct: float
    drop_15m_pct: float
    drop_30m_pct: float
    peak_equity: float
    trough_equity: float
    force_retrain: bool
    reason: str


class DrawdownGuard:
    """Tracks equity velocity and trade quality; switches trading temperament."""

    CAUTION_DRAWDOWN_PCT = 4.0
    CHILL_DRAWDOWN_PCT = 8.0
    CHILL_DROP_30M_PCT = 4.0
    CAUTION_DROP_15M_PCT = 2.5

    CHILL_MIN_SECONDS = 900  # 15 min minimum chill before recovery eligible
    RECOVERY_RAMP_PER_STABLE_TICK = 0.04
    RECOVERY_RAMP_PER_WINNING_TICK = 0.07
    STABLE_TICKS_FOR_RECOVERY = 8

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "drawdown_guard.json"
        self._samples: deque[tuple[float, float]] = deque(maxlen=240)  # ~2h @ 30s
        self.mode = AccountMode.NORMAL
        self.heat_factor = 1.0
        self.peak_equity = 0.0
        self.trough_equity = 0.0
        self.chill_entered_at = 0.0
        self.stable_ticks = 0
        self._force_retrain = False
        self._last_reason = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.mode = AccountMode(raw.get("mode", AccountMode.NORMAL.value))
            self.heat_factor = float(raw.get("heat_factor", 1.0))
            self.peak_equity = float(raw.get("peak_equity", 0))
            self.trough_equity = float(raw.get("trough_equity", 0))
            self.chill_entered_at = float(raw.get("chill_entered_at", 0))
            self.stable_ticks = int(raw.get("stable_ticks", 0))
            for item in raw.get("samples", [])[-240:]:
                self._samples.append((float(item[0]), float(item[1])))
        except Exception:
            log.warning("drawdown_guard state corrupt — resetting")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "mode": self.mode.value,
                    "heat_factor": round(self.heat_factor, 4),
                    "peak_equity": round(self.peak_equity, 6),
                    "trough_equity": round(self.trough_equity, 6),
                    "chill_entered_at": self.chill_entered_at,
                    "stable_ticks": self.stable_ticks,
                    "samples": list(self._samples)[-120:],
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _drop_over_window(self, now: float, window_sec: float) -> float:
        if not self._samples:
            return 0.0
        cutoff = now - window_sec
        old = [eq for ts, eq in self._samples if ts <= cutoff]
        if not old:
            return 0.0
        start = old[-1]
        current = self._samples[-1][1]
        if start <= 0:
            return 0.0
        return max(0.0, (start - current) / start * 100.0)

    def tick(
        self,
        equity: float,
        win_rate: float,
        profit_factor: float,
        consecutive_losses: int = 0,
        *,
        chill_drawdown_pct: float | None = None,
        caution_drawdown_pct: float | None = None,
        chill_drop_30m_pct: float | None = None,
    ) -> GuardSnapshot:
        chill_dd = chill_drawdown_pct if chill_drawdown_pct is not None else self.CHILL_DRAWDOWN_PCT
        caution_dd = caution_drawdown_pct if caution_drawdown_pct is not None else self.CAUTION_DRAWDOWN_PCT
        chill_30m = chill_drop_30m_pct if chill_drop_30m_pct is not None else self.CHILL_DROP_30M_PCT
        now = time.time()
        self._samples.append((now, equity))

        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.trough_equity <= 0 or equity < self.trough_equity:
            self.trough_equity = equity

        drawdown_pct = 0.0
        if self.peak_equity > 0:
            drawdown_pct = max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

        drop_15m = self._drop_over_window(now, 900)
        drop_30m = self._drop_over_window(now, 1800)

        prev_mode = self.mode
        self._force_retrain = False
        reason = ""

        # --- Enter worse modes ---
        if (
            drawdown_pct >= chill_dd
            or drop_30m >= chill_30m
            or (profit_factor < 0.55 and win_rate < 0.32 and consecutive_losses >= 3)
            or consecutive_losses >= 5
        ):
            if self.mode != AccountMode.CHILL:
                self.mode = AccountMode.CHILL
                self.chill_entered_at = now
                self.heat_factor = 0.0
                self.stable_ticks = 0
                self._force_retrain = True
                reason = (
                    f"CHILL: dd={drawdown_pct:.1f}% drop30m={drop_30m:.1f}% "
                    f"pf={profit_factor:.2f} losses={consecutive_losses}"
                )
                log.warning(reason)
        elif (
            drawdown_pct >= caution_dd
            or drop_15m >= self.CAUTION_DROP_15M_PCT
            or (profit_factor < 0.85 and consecutive_losses >= 3)
        ):
            if self.mode in (AccountMode.NORMAL, AccountMode.RECOVERING):
                self.mode = AccountMode.CAUTION
                self.heat_factor = min(self.heat_factor, 0.55)
                reason = f"CAUTION: dd={drawdown_pct:.1f}% drop15m={drop_15m:.1f}%"
                log.warning(reason)

        # --- Chill → recovering ---
        elif self.mode == AccountMode.CHILL:
            chill_age = now - self.chill_entered_at
            recovered_from_trough = False
            if self.trough_equity > 0 and equity > self.trough_equity:
                bounce = (equity - self.trough_equity) / self.trough_equity * 100.0
                recovered_from_trough = bounce >= 1.5

            if chill_age >= self.CHILL_MIN_SECONDS and (
                drawdown_pct < caution_dd
                or recovered_from_trough
                or (profit_factor >= 1.0 and win_rate >= 0.45)
            ):
                self.mode = AccountMode.RECOVERING
                self.heat_factor = max(self.heat_factor, 0.2)
                self.stable_ticks = 0
                reason = "RECOVERING: composure — slow ramp"
                log.info(reason)

        # --- Recovering → normal (heat ramp) ---
        elif self.mode == AccountMode.RECOVERING:
            new_low = len(self._samples) >= 2 and equity < self._samples[-2][1] * 0.998
            if new_low:
                self.stable_ticks = 0
                self.heat_factor = max(0.15, self.heat_factor - 0.08)
            else:
                self.stable_ticks += 1
                ramp = self.RECOVERY_RAMP_PER_WINNING_TICK if profit_factor >= 1.0 else self.RECOVERY_RAMP_PER_STABLE_TICK
                if self.stable_ticks >= 2:
                    self.heat_factor = min(1.0, self.heat_factor + ramp)

            if self.heat_factor >= 0.98 and drawdown_pct < 2.0 and profit_factor >= 0.95:
                self.mode = AccountMode.NORMAL
                self.heat_factor = 1.0
                self.peak_equity = equity
                reason = "NORMAL: heat fully restored"
                log.info(reason)
            elif drawdown_pct >= chill_dd:
                self.mode = AccountMode.CHILL
                self.chill_entered_at = now
                self.heat_factor = 0.0
                self._force_retrain = True
                reason = "back to CHILL — drawdown worsened"
                log.warning(reason)

        # --- Caution → normal or recovering ---
        elif self.mode == AccountMode.CAUTION:
            if drawdown_pct < 2.0 and profit_factor >= 1.0 and win_rate >= 0.4:
                self.mode = AccountMode.NORMAL
                self.heat_factor = 1.0
                reason = "NORMAL: caution cleared"
                log.info(reason)
            else:
                self.heat_factor = min(0.55, max(0.35, 1.0 - drawdown_pct / 20.0))

        # --- Normal: full heat when healthy ---
        elif self.mode == AccountMode.NORMAL:
            self.heat_factor = 1.0
            if equity >= self.peak_equity * 0.999:
                self.peak_equity = equity

        allow = self.mode not in (AccountMode.CHILL,)
        if self.mode == AccountMode.RECOVERING and self.heat_factor < 0.25:
            allow = False

        if prev_mode != self.mode or reason:
            self._last_reason = reason or self._last_reason

        self._save()

        return GuardSnapshot(
            mode=self.mode,
            heat_factor=round(self.heat_factor, 3),
            allow_new_entries=allow,
            drawdown_pct=round(drawdown_pct, 2),
            drop_15m_pct=round(drop_15m, 2),
            drop_30m_pct=round(drop_30m, 2),
            peak_equity=round(self.peak_equity, 4),
            trough_equity=round(self.trough_equity, 4),
            force_retrain=self._force_retrain,
            reason=self._last_reason,
        )

    def consume_retrain_flag(self) -> bool:
        if self._force_retrain:
            self._force_retrain = False
            return True
        return False
