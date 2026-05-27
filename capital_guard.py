"""
Capital preservation — daily loss ceiling, drawdown composure, exposure limits.

These gates run ALWAYS (even when UNRESTRICTED_TRADING=true). Steward still
manages open positions; only new entries are blocked.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from drawdown_guard import AccountMode, DrawdownGuard, GuardSnapshot

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)


@dataclass
class CapitalSnapshot:
    allow_entries: bool
    heat_factor: float
    mode: str
    daily_loss_pct: float
    drawdown_pct: float
    reason: str
    force_retrain: bool


class DailyLossGuard:
    """UTC-day session: stop new entries after max daily loss."""

    def __init__(self, state_dir, max_daily_loss_pct: float) -> None:
        self.path = state_dir / "daily_session.json"
        pct = max_daily_loss_pct
        self.max_daily_loss_pct = pct * 100.0 if pct <= 1.0 else pct
        self._day = ""
        self._start_equity = 0.0

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._day = str(raw.get("day", ""))
            self._start_equity = float(raw.get("start_equity", 0))
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"day": self._day, "start_equity": round(self._start_equity, 6), "updated": time.time()},
                indent=2,
            ),
            encoding="utf-8",
        )

    def tick(self, equity: float) -> tuple[bool, float, str]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day or self._start_equity <= 0:
            self._day = today
            self._start_equity = max(equity, 0.01)
            self._save()
            return True, 0.0, ""

        if self._start_equity <= 0:
            return True, 0.0, ""

        loss_pct = max(0.0, (self._start_equity - equity) / self._start_equity * 100.0)
        if loss_pct >= self.max_daily_loss_pct:
            return (
                False,
                loss_pct,
                f"daily loss {loss_pct:.1f}% >= cap {self.max_daily_loss_pct:.1f}% (session start ${self._start_equity:.2f})",
            )
        return True, loss_pct, ""


class LossStreakPause:
    """After N consecutive losses, pause entries for cooldown minutes."""

    def __init__(self, state_dir, streak_limit: int = 3, cooldown_minutes: int = 45) -> None:
        self.path = state_dir / "loss_streak_pause.json"
        self.streak_limit = streak_limit
        self.cooldown_sec = cooldown_minutes * 60
        self._paused_until = 0.0

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._paused_until = float(json.loads(self.path.read_text(encoding="utf-8")).get("paused_until", 0))
        except Exception:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"paused_until": self._paused_until}), encoding="utf-8")

    def on_loss_streak(self, streak: int) -> None:
        if streak >= self.streak_limit and time.time() >= self._paused_until:
            self._paused_until = time.time() + self.cooldown_sec
            self._save()
            log.warning(
                "LOSS STREAK PAUSE: %d losses — no new entries for %d min",
                streak,
                self.cooldown_sec // 60,
            )

    def allowed(self) -> tuple[bool, str]:
        self._load()
        if time.time() < self._paused_until:
            left = int(self._paused_until - time.time())
            return False, f"loss-streak cooldown ({left // 60}m {left % 60}s left)"
        return True, ""


class CapitalGuard:
    """Single entry point for all capital-preservation gates."""

    def __init__(self, state_dir, settings: "Settings") -> None:
        self.settings = settings
        self.drawdown = DrawdownGuard(state_dir)
        self.daily = DailyLossGuard(state_dir, settings.max_daily_loss_pct)
        self.streak_pause = LossStreakPause(
            state_dir,
            streak_limit=3 if settings.small_account_threshold > 0 else 4,
            cooldown_minutes=45 if settings.small_account_threshold > 0 else 30,
        )
        self.daily._load()
        self.streak_pause._load()

    def tick(
        self,
        equity: float,
        *,
        win_rate: float,
        profit_factor: float,
        consecutive_losses: int,
    ) -> CapitalSnapshot:
        micro = equity > 0 and equity < self.settings.small_account_threshold

        if micro:
            chill_dd, caution_dd, chill_30m = 5.0, 2.5, 2.0
        else:
            chill_dd, caution_dd, chill_30m = 8.0, 4.0, 4.0

        snap = self.drawdown.tick(
            equity,
            win_rate,
            profit_factor,
            consecutive_losses,
            chill_drawdown_pct=chill_dd,
            caution_drawdown_pct=caution_dd,
            chill_drop_30m_pct=chill_30m,
        )
        self.streak_pause.on_loss_streak(consecutive_losses)

        daily_ok, daily_loss, daily_reason = self.daily.tick(equity)
        streak_ok, streak_reason = self.streak_pause.allowed()

        allow = snap.allow_new_entries and daily_ok and streak_ok
        reasons = [r for r in (snap.reason, daily_reason, streak_reason) if r]
        reason = " | ".join(reasons) if reasons else snap.mode.value

        if not allow and reason:
            log.info(
                "CAPITAL GUARD block entries | mode=%s heat=%.0f%% dd=%.1f%% daily_loss=%.1f%% | %s",
                snap.mode.value,
                snap.heat_factor * 100,
                snap.drawdown_pct,
                daily_loss,
                reason,
            )

        return CapitalSnapshot(
            allow_entries=allow,
            heat_factor=snap.heat_factor if allow else 0.0,
            mode=snap.mode.value,
            daily_loss_pct=round(daily_loss, 2),
            drawdown_pct=snap.drawdown_pct,
            reason=reason,
            force_retrain=snap.force_retrain,
        )

    def consume_retrain_flag(self) -> bool:
        return self.drawdown.consume_retrain_flag()


def same_side_exposure_ok(
    open_positions: dict,
    side: str,
    *,
    max_same_side: int,
) -> tuple[bool, str]:
    if max_same_side <= 0:
        return True, ""
    side = side.lower()
    count = sum(1 for p in open_positions.values() if str(p.get("side", "")).lower() == side)
    if count >= max_same_side:
        return False, f"max {max_same_side} {side} positions already open ({count})"
    return True, ""


def signal_quality_ok(decision, settings: "Settings", equity: float) -> tuple[bool, str]:
    """Extra filters for micro accounts — quality over quantity."""
    micro = equity > 0 and equity < settings.small_account_threshold
    if settings.htf_required and not getattr(decision, "htf_aligned", True):
        return False, "HTF not aligned"

    agree = int(getattr(decision, "confluence_agreeing", 0) or 0)
    min_agree = settings.min_confluence_agreeing
    if micro:
        min_agree = max(min_agree, 3)
    if agree < min_agree:
        return False, f"confluence agreeing={agree} < {min_agree}"

    vol = float(getattr(decision, "volume_ratio", 0) or 0)
    if vol < settings.min_volume_ratio:
        return False, f"volume ratio {vol:.2f} < {settings.min_volume_ratio}"

    if decision.signal.value == "long":
        fr = getattr(decision, "funding_rate", None)
        if fr is not None and fr > settings.max_funding_long:
            return False, f"funding {fr:.4f} too high for long"
    elif decision.signal.value == "short":
        fr = getattr(decision, "funding_rate", None)
        if fr is not None and fr < settings.min_funding_short:
            return False, f"funding {fr:.4f} too low for short"

    sp = decision.take_pct / max(decision.stop_pct, 1e-9)
    if settings.scalp_3r_mode and sp < settings.scalp_3r_min_rr * 0.95:
        return False, f"R:R {sp:.2f} below 3R floor"

    return True, ""
