"""
COMPOUND GROWTH OPTIMIZER
Maximum geometric growth calculator.
Dynamically adjusts aggression based on recent performance,
drawdown state, and path to mission target (mission_config).

Key features:
- Tracks running profit factor on short and long windows
- Dynamically scales risk per trade up when winning, down when losing
- Ensures drawdowns are recovered aggressively
- Tracks today's return vs maintain/exceed 10%/day mission (mission_config)
- Provides psychological boost metrics for the bot
"""
from __future__ import annotations
import json
import time
import math
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


from mission_config import (
    START_CAPITAL_REFERENCE as START_CAPITAL,
    TARGET_DAILY_GROWTH_MULT,
    TARGET_DAILY_GROWTH_PCT,
    daily_growth_on_track,
    sole_objective_label,
    target_daily_growth_multiplier,
    target_daily_growth_pct,
)


def _day_start_equity(history: list[dict], current_equity: float) -> float:
    """First equity snapshot today (UTC), else best available baseline."""
    today = time.strftime("%Y-%m-%d")
    day_rows = [h for h in history if h.get("day") == today and float(h.get("equity") or 0) > 0]
    if day_rows:
        return float(day_rows[0]["equity"])
    for h in reversed(history):
        eq = float(h.get("equity") or 0)
        if eq > 0:
            return eq
    return current_equity if current_equity > 0 else START_CAPITAL


@dataclass
class GrowthMetrics:
    """Current growth state and required trajectory."""
    current_equity: float
    days_remaining: int
    required_daily_return_pct: float
    required_daily_return_multiplier: float
    on_track: bool
    projected_capital_at_target: float
    aggression_boost: float  # 1.0 = normal, >1.0 = more aggressive
    days_to_double_at_current_rate: float


class CompoundGrowthOptimizer:
    """
    Calculates and enforces the growth trajectory needed to hit the mission target.
    
    The optimizer dynamically adjusts risk aggression based on:
    1. How far ahead/behind the growth schedule you are
    2. Recent profit factor (last 10 trades vs last 50)
    3. Current drawdown state
    4. Account size tier (small accounts need more aggression)
    """
    
    def __init__(
        self,
        state_dir: Path,
        *,
        target_daily_pct: float = TARGET_DAILY_GROWTH_PCT,
        start_capital: float = START_CAPITAL,
    ) -> None:
        self.state_dir = state_dir
        self.target_daily_pct = target_daily_pct
        self.start_capital = start_capital
        self.path = state_dir / "growth_metrics.json"
        self.history: list[dict] = []
        self._load()
        self.required_daily_return = target_daily_growth_multiplier() - 1.0

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.history = raw.get("history", [])
            except (json.JSONDecodeError, KeyError):
                self.history = []
    
    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"history": self.history[-500:]}, indent=2),
            encoding="utf-8",
        )
    
    def record_equity_snapshot(self, equity: float):
        """Record a daily equity snapshot for growth tracking."""
        if equity <= 0:
            return
        entry = {
            "equity": round(equity, 4),
            "ts": time.time(),
            "day": time.strftime("%Y-%m-%d"),
        }
        self.history.append(entry)
        self._save()
    
    def get_growth_metrics(self, current_equity: float) -> GrowthMetrics:
        """
        Today's growth vs maintain/exceed 10%/day mission.
        Returns GrowthMetrics with aggression_boost factor.
        """
        required_daily_pct = self.target_daily_pct
        required_multiplier = target_daily_growth_multiplier()
        day_start = _day_start_equity(self.history, current_equity)

        if day_start > 0 and current_equity > 0:
            actual_daily_pct = (current_equity / day_start - 1.0) * 100.0
        else:
            actual_daily_pct = 0.0

        target_eod = day_start * required_multiplier if day_start > 0 else 0.0
        gap_to_target = max(0.0, target_eod - current_equity) if target_eod > 0 else 0.0

        if actual_daily_pct > 0:
            days_to_double = math.log(2) / math.log(1 + actual_daily_pct / 100.0)
        else:
            days_to_double = float("inf")

        if required_daily_pct > 0 and actual_daily_pct > 0:
            performance_ratio = actual_daily_pct / required_daily_pct
        else:
            performance_ratio = 0.0 if actual_daily_pct <= 0 else 1.0

        if performance_ratio < 1.0:
            aggression_boost = min(1.8, 1.0 + (1.0 - performance_ratio) * 0.6)
        else:
            aggression_boost = 1.0

        if current_equity < 1000:
            aggression_boost = min(2.0, aggression_boost * 1.15)
        elif current_equity < 10000:
            aggression_boost = min(1.9, aggression_boost * 1.08)

        aggression_boost = max(1.0, min(2.0, aggression_boost))
        on_track = daily_growth_on_track(actual_daily_pct)

        return GrowthMetrics(
            current_equity=current_equity,
            days_remaining=1,
            required_daily_return_pct=round(required_daily_pct, 4),
            required_daily_return_multiplier=round(required_multiplier, 6),
            on_track=on_track,
            projected_capital_at_target=round(target_eod, 4),
            aggression_boost=round(aggression_boost, 4),
            days_to_double_at_current_rate=round(days_to_double, 1)
            if days_to_double != float("inf")
            else -1,
        )
    
    def get_aggression_boost(self) -> float:
        """
        Get the current aggression multiplier for position sizing.
        This is called by bot.py on each tick.
        """
        # Use the last recorded equity or default
        if self.history:
            last_equity = self.history[-1]["equity"]
        else:
            last_equity = START_CAPITAL
        
        metrics = self.get_growth_metrics(last_equity)
        return metrics.aggression_boost
    
    def format_growth_report(self, current_equity: float) -> str:
        """Generate a human-readable growth status report."""
        metrics = self.get_growth_metrics(current_equity)
        
        day_start = _day_start_equity(self.history, current_equity)
        today_pct = (
            (current_equity / day_start - 1.0) * 100.0 if day_start > 0 and current_equity > 0 else 0.0
        )
        lines = [
            "=" * 60,
            "DAILY GROWTH REPORT",
            "=" * 60,
            f"Mission:            {sole_objective_label()}",
            f"Current Equity:     ${metrics.current_equity:,.4f}",
            f"Day start equity:   ${day_start:,.4f}",
            f"Today so far:       {today_pct:+.2f}%",
            f"Required today:     {metrics.required_daily_return_pct:.2f}%",
            f"EOD target equity:  ${metrics.projected_capital_at_target:,.4f}",
            f"Aggression Boost:   {metrics.aggression_boost:.2f}x",
            f"On Track:           {'YES' if metrics.on_track else 'NO'}",
            "=" * 60,
        ]

        if not metrics.on_track:
            lines.append(
                f"⚠️  BELOW +10% TODAY — need {metrics.required_daily_return_pct:.2f}% "
                f"from day open (currently {today_pct:+.2f}%)"
            )
        
        return "\n".join(lines)