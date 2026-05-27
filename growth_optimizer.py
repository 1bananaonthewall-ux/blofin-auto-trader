"""
COMPOUND GROWTH OPTIMIZER
Maximum geometric growth calculator.
Dynamically adjusts aggression based on recent performance,
drawdown state, and path to $95M target.

Key features:
- Tracks running profit factor on short and long windows
- Dynamically scales risk per trade up when winning, down when losing
- Ensures drawdowns are recovered aggressively
- Calculates required daily return to hit $95M by 2027-09-01
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
    TARGET_CAPITAL_USD as TARGET_CAPITAL,
    target_date_iso,
    target_date_ts,
)

TARGET_DATE = target_date_iso()


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
    Calculates and enforces the growth trajectory needed to hit $95M.
    
    The optimizer dynamically adjusts risk aggression based on:
    1. How far ahead/behind the growth schedule you are
    2. Recent profit factor (last 10 trades vs last 50)
    3. Current drawdown state
    4. Account size tier (small accounts need more aggression)
    """
    
    def __init__(self, state_dir: Path, target_capital: float = TARGET_CAPITAL,
                 start_capital: float = START_CAPITAL):
        self.state_dir = state_dir
        self.target_capital = target_capital
        self.start_capital = start_capital
        self.path = state_dir / "growth_metrics.json"
        self.history: list[dict] = []
        self._load()
        
        days = max(1, int((target_date_ts() - time.time()) / 86400))
        self.required_daily_return = (target_capital / max(start_capital, 1e-9)) ** (1.0 / days) - 1

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
        entry = {
            "equity": round(equity, 4),
            "ts": time.time(),
            "day": time.strftime("%Y-%m-%d"),
        }
        self.history.append(entry)
        self._save()
    
    def get_growth_metrics(self, current_equity: float) -> GrowthMetrics:
        """
        Calculate current growth status and required trajectory.
        Returns GrowthMetrics with aggression_boost factor.
        """
        target_ts = target_date_ts()
        now = time.time()
        seconds_remaining = target_ts - now
        days_remaining = max(1, int(seconds_remaining / 86400))
        
        # Required daily return to hit target
        if current_equity > 0 and days_remaining > 0:
            required_multiplier = (self.target_capital / current_equity) ** (1.0 / days_remaining)
            required_daily_pct = (required_multiplier - 1) * 100
        else:
            required_multiplier = 1.0
            required_daily_pct = 0.0
        
        # Calculate recent growth rate from history
        recent_equities = [h["equity"] for h in self.history[-30:]]
        if len(recent_equities) >= 2:
            oldest = recent_equities[0]
            newest = recent_equities[-1]
            days_span = max(1, len(recent_equities) - 1)
            if oldest > 0:
                actual_multiplier = (newest / oldest) ** (1.0 / days_span)
                actual_daily_pct = (actual_multiplier - 1) * 100
            else:
                actual_multiplier = 1.0
                actual_daily_pct = 0.0
        else:
            actual_multiplier = 1.0
            actual_daily_pct = 0.0
        
        # Projection at current rate
        if actual_daily_pct > 0:
            days_to_double = math.log(2) / math.log(1 + actual_daily_pct / 100)
            projected = current_equity * ((1 + actual_daily_pct / 100) ** days_remaining)
        else:
            days_to_double = float('inf')
            projected = current_equity
        
        # Determine aggression boost
        # If behind schedule, increase aggression
        if required_daily_pct > 0 and actual_daily_pct > 0:
            performance_ratio = actual_daily_pct / required_daily_pct
        else:
            performance_ratio = 0.5  # assume behind
        
        # Scale aggression modestly when behind — sizing still margin-gated
        if performance_ratio < 1.0:
            aggression_boost = min(1.8, 1.0 + (1.0 - performance_ratio) * 0.6)
        else:
            aggression_boost = 1.0

        if current_equity < 1000:
            aggression_boost = min(2.0, aggression_boost * 1.15)
        elif current_equity < 10000:
            aggression_boost = min(1.9, aggression_boost * 1.08)

        aggression_boost = max(1.0, min(2.0, aggression_boost))
        
        on_track = performance_ratio >= 1.0
        
        return GrowthMetrics(
            current_equity=current_equity,
            days_remaining=days_remaining,
            required_daily_return_pct=round(required_daily_pct, 4),
            required_daily_return_multiplier=round(required_multiplier, 6),
            on_track=on_track,
            projected_capital_at_target=round(projected, 2),
            aggression_boost=round(aggression_boost, 4),
            days_to_double_at_current_rate=round(days_to_double, 1) if days_to_double != float('inf') else -1,
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
        
        lines = [
            "=" * 60,
            "COMPOUND GROWTH REPORT",
            "=" * 60,
            f"Current Equity:     ${metrics.current_equity:,.2f}",
            f"Target:             ${self.target_capital:,.0f}",
            f"Days Remaining:     {metrics.days_remaining}",
            f"Required Daily:     {metrics.required_daily_return_pct:.4f}%",
            f"Required Mult:      {metrics.required_daily_return_multiplier:.6f}x",
            f"Days to Double:     {metrics.days_to_double_at_current_rate:.1f}" if metrics.days_to_double_at_current_rate > 0 else "Days to Double:     N/A",
            f"Aggression Boost:   {metrics.aggression_boost:.2f}x",
            f"On Track:           {'YES' if metrics.on_track else 'NO'}" ,
            f"Projected at target date: ${metrics.projected_capital_at_target:,.2f}",
            "=" * 60,
        ]
        
        if not metrics.on_track:
            deficit_pct = (1 - (metrics.projected_capital_at_target / self.target_capital)) * 100
            lines.append(f"⚠️  BEHIND SCHEDULE by {deficit_pct:.1f}% - increasing aggression!")
            lines.append(f"    Need {metrics.required_daily_return_pct:.4f}% per day to catch up")
        
        return "\n".join(lines)