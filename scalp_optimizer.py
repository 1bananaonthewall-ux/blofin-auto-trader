"""
15-minute scalp optimizer — tune winner gate + pacing from live results.

No pause/chill rails: only adjusts how picky the bot is and how fast it fires
when setups pass. Runs inside bot.py every OPTIMIZER_INTERVAL_SECONDS (default 900).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

TUNING_PATH_NAME = "scalp_tuning.json"
REPORT_PATH_NAME = "optimizer_report.jsonl"

_active: "ScalpTuning | None" = None


@dataclass
class ScalpTuning:
    """Runtime offsets applied on top of .env winner/scalp settings."""

    confluence_delta: float = 0.0
    agreeing_delta: int = 0
    ml_conf_delta: float = 0.0
    volume_delta: float = 0.0
    min_score_delta: float = 0.0
    entry_gap_seconds: float | None = None
    symbol_cooldown_minutes: float | None = None
    updated_ts: float = 0.0
    trades_last_hour: int = 0
    win_rate_recent: float = 0.0
    profit_factor_recent: float = 1.0
    equity_delta_15m_pct: float = 0.0
    action: str = "hold"
    notes: str = ""


@dataclass
class OptimizerReport:
    ts: float
    trades_15m: int
    trades_60m: int
    win_rate: float
    profit_factor: float
    equity_delta_15m_pct: float
    action: str
    notes: str
    tuning: ScalpTuning

    @property
    def summary(self) -> str:
        return (
            f"tph={self.trades_60m} wr={self.win_rate:.0%} pf={self.profit_factor:.2f} "
            f"eq15m={self.equity_delta_15m_pct:+.2f}% -> {self.action} | {self.notes}"
        )


def get_active_tuning() -> ScalpTuning:
    global _active
    if _active is None:
        _active = ScalpTuning()
    return _active


def _parse_ts(raw: str | float | int) -> float:
    if isinstance(raw, (int, float)):
        return float(raw) / 1000.0 if raw > 1e12 else float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _count_journal_opens(path: Path, since_ts: float) -> int:
    if not path.exists():
        return 0
    n = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-3000:]:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "open":
                continue
            if _parse_ts(row.get("ts", 0)) >= since_ts:
                n += 1
    except Exception:
        pass
    return n


def _recent_trade_stats(state_dir: Path, since_ts: float) -> tuple[float, float, int]:
    path = state_dir / "profitability.json"
    if not path.exists():
        return 0.5, 1.0, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trades = raw.get("trades", [])
        recent = [t for t in trades if _parse_ts(t.get("ts", t.get("closed_ts", 0))) >= since_ts]
        if not recent:
            recent = trades[-20:]
        if not recent:
            return 0.5, 1.0, 0
        wins = sum(1 for t in recent if float(t.get("net_pnl", 0)) > 0)
        wr = wins / len(recent)
        gp = sum(float(t["net_pnl"]) for t in recent if float(t.get("net_pnl", 0)) > 0)
        gl = abs(sum(float(t["net_pnl"]) for t in recent if float(t.get("net_pnl", 0)) < 0))
        pf = (gp / gl) if gl > 0 else (2.0 if gp > 0 else 1.0)
        return wr, pf, len(recent)
    except Exception:
        return 0.5, 1.0, 0


def _equity_delta_pct(state_dir: Path, window_sec: float) -> float:
    path = state_dir / "equity_ticks.jsonl"
    if not path.exists():
        return 0.0
    now = time.time()
    cutoff = now - window_sec
    points: list[tuple[float, float]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-800:]:
            if not line.strip():
                continue
            row = json.loads(line)
            ts = float(row.get("ts", 0))
            if ts >= cutoff:
                points.append((ts, float(row.get("equity", 0))))
    except Exception:
        return 0.0
    if len(points) < 2:
        return 0.0
    start = points[0][1]
    end = points[-1][1]
    if start <= 0:
        return 0.0
    return (end - start) / start * 100.0


@dataclass
class EffectiveWinnerThresholds:
    min_confluence: float
    min_agreeing: int
    max_opposing: int
    min_ml_confidence: float
    min_volume_ratio: float
    min_score: float
    elite_score: float
    apex_score: float


def effective_winner_thresholds(settings: "Settings", tuning: ScalpTuning | None = None) -> EffectiveWinnerThresholds:
    t = tuning or get_active_tuning()
    base_cf = settings.winner_min_confluence
    base_agree = settings.winner_min_agreeing
    base_ml = settings.winner_min_ml_confidence
    base_vol = settings.winner_min_volume_ratio
    base_score = settings.winner_min_score
    base_elite = settings.winner_elite_score
    base_apex = settings.winner_apex_score
    quality_first = getattr(settings, "optimizer_quality_first", True)
    cf_d = t.confluence_delta if not quality_first else max(0.0, t.confluence_delta)
    agree_d = t.agreeing_delta if not quality_first else max(0, t.agreeing_delta)
    ml_d = t.ml_conf_delta if not quality_first else max(0.0, t.ml_conf_delta)
    score_d = t.min_score_delta if not quality_first else max(0.0, t.min_score_delta)

    cf = max(0.52, min(0.72, base_cf + cf_d))
    agree = max(4, min(9, base_agree + agree_d))
    ml = max(0.68, min(0.88, base_ml + ml_d))
    vol = max(0.95, min(1.8, base_vol + t.volume_delta))
    score = max(0.48, min(0.72, base_score + score_d))
    elite = max(score + 0.08, min(0.82, base_elite + score_d * 0.5))
    apex = max(elite + 0.05, min(0.88, base_apex + score_d * 0.35))

    return EffectiveWinnerThresholds(
        min_confluence=cf,
        min_agreeing=agree,
        max_opposing=settings.winner_max_opposing,
        min_ml_confidence=ml,
        min_volume_ratio=vol,
        min_score=score,
        elite_score=elite,
        apex_score=apex,
    )


def effective_entry_gap(settings: "Settings", tuning: ScalpTuning | None = None) -> float:
    t = tuning or get_active_tuning()
    base = settings.scalp_entry_gap_seconds
    if t.entry_gap_seconds is not None:
        return max(15.0, min(75.0, t.entry_gap_seconds))
    return max(15.0, min(75.0, base))


def effective_cooldown_minutes(settings: "Settings", tuning: ScalpTuning | None = None) -> float:
    t = tuning or get_active_tuning()
    base = float(settings.scalp_cooldown_minutes)
    if t.symbol_cooldown_minutes is not None:
        return max(2.0, min(15.0, t.symbol_cooldown_minutes))
    return base


class ScalpOptimizer:
    def __init__(self, state_dir: Path, settings: "Settings") -> None:
        self.state_dir = state_dir
        self.settings = settings
        self.tuning_path = state_dir / TUNING_PATH_NAME
        self.report_path = state_dir / REPORT_PATH_NAME
        self.interval = float(getattr(settings, "optimizer_interval_seconds", 900))
        self.target_min_tph = int(getattr(settings, "optimizer_target_min_tph", 3))
        self.target_max_tph = int(getattr(settings, "optimizer_target_max_tph", 12))
        self._throughput_mode = not getattr(settings, "optimizer_quality_first", True)
        self._last_run = 0.0
        self.tuning = ScalpTuning()
        self._load()

    def _load(self) -> None:
        global _active
        if not self.tuning_path.exists():
            _active = self.tuning
            return
        try:
            raw = json.loads(self.tuning_path.read_text(encoding="utf-8"))
            self.tuning = ScalpTuning(**{k: v for k, v in raw.items() if k in ScalpTuning.__dataclass_fields__})
            self._last_run = float(raw.get("updated_ts", 0))
        except Exception:
            pass
        _active = self.tuning

    def _save(self) -> None:
        global _active
        self.tuning.updated_ts = time.time()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(self.tuning)
        self.tuning_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _active = self.tuning

    def _append_report(self, report: OptimizerReport) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trades_15m": report.trades_15m,
            "trades_60m": report.trades_60m,
            "win_rate": round(report.win_rate, 4),
            "profit_factor": round(report.profit_factor, 3),
            "equity_delta_15m_pct": round(report.equity_delta_15m_pct, 4),
            "action": report.action,
            "notes": report.notes,
            "tuning": asdict(report.tuning),
        }
        with self.report_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def maybe_optimize(
        self,
        equity: float,
        *,
        win_rate: float = 0.5,
        profit_factor: float = 1.0,
        ml_ready: bool = True,
        force: bool = False,
    ) -> OptimizerReport | None:
        now = time.time()
        if not force and (now - self._last_run) < self.interval:
            return None
        if not getattr(self.settings, "optimizer_enabled", True):
            return None

        self._last_run = now
        journal = self.state_dir / "trades.jsonl"
        t15 = now - 900
        t60 = now - 3600

        opens_15m = _count_journal_opens(journal, t15)
        opens_60m = _count_journal_opens(journal, t60)
        wr, pf, closed_n = _recent_trade_stats(self.state_dir, t60)
        if closed_n == 0:
            wr, pf = win_rate, profit_factor
        eq15 = _equity_delta_pct(self.state_dir, 900)

        t = self.tuning
        t.trades_last_hour = opens_60m
        t.win_rate_recent = wr
        t.profit_factor_recent = pf
        t.equity_delta_15m_pct = eq15
        action = "hold"
        notes: list[str] = []

        base_gap = self.settings.scalp_entry_gap_seconds
        base_cd = float(self.settings.scalp_cooldown_minutes)

        quality_first = getattr(self.settings, "optimizer_quality_first", True) and not self._throughput_mode

        # Throughput: starved but not bleeding → pace up (quality-first never loosens gates)
        if opens_60m < self.target_min_tph and eq15 > -2.5:
            gap = (t.entry_gap_seconds or base_gap) - 4.0
            t.entry_gap_seconds = max(8.0, gap)
            cd = (t.symbol_cooldown_minutes or base_cd) - 1.0
            t.symbol_cooldown_minutes = max(2.0, cd)
            if quality_first:
                action = "pace_up_quality"
                notes.append(f"starved<{self.target_min_tph}/hr pace-only (gates fixed)")
            else:
                t.confluence_delta = max(-0.08, t.confluence_delta - 0.015)
                t.agreeing_delta = max(-2, t.agreeing_delta - 1)
                t.ml_conf_delta = max(-0.05, t.ml_conf_delta - 0.01)
                t.min_score_delta = max(-0.06, t.min_score_delta - 0.02)
                action = "loosen_throughput"
                notes.append(f"starved<{self.target_min_tph}/hr")

        # Quality bleed → tighten (still trading, just pickier)
        elif wr < 0.40 or pf < 0.85 or eq15 < -3.0:
            t.confluence_delta = min(0.08, t.confluence_delta + 0.02)
            t.agreeing_delta = min(2, t.agreeing_delta + 1)
            t.ml_conf_delta = min(0.06, t.ml_conf_delta + 0.015)
            t.min_score_delta = min(0.06, t.min_score_delta + 0.02)
            gap = (t.entry_gap_seconds or base_gap) + 3.0
            t.entry_gap_seconds = min(60.0, gap)
            action = "tighten_quality"
            notes.append(f"wr={wr:.0%} pf={pf:.2f}")

        # Winning hot → slight loosen to capture more A+ hours
        elif wr >= 0.52 and pf >= 1.15 and opens_60m < self.target_max_tph:
            t.confluence_delta = max(-0.06, t.confluence_delta - 0.008)
            t.min_score_delta = max(-0.04, t.min_score_delta - 0.01)
            gap = (t.entry_gap_seconds or base_gap) - 2.0
            t.entry_gap_seconds = max(18.0, gap)
            action = "accelerate_hot"
            notes.append("winning streak")

        # Over-trading weak → tighten pace only
        elif opens_60m > self.target_max_tph and wr < 0.48:
            gap = (t.entry_gap_seconds or base_gap) + 5.0
            t.entry_gap_seconds = min(65.0, gap)
            t.confluence_delta = min(0.06, t.confluence_delta + 0.01)
            action = "slow_overtrade"
            notes.append(f"overtrading>{self.target_max_tph}/hr")

        if not ml_ready and opens_60m < 1:
            notes.append("ML not ready — run train_model.py for better picks")

        t.action = action
        t.notes = "; ".join(notes) if notes else "steady"
        self._save()

        report = OptimizerReport(
            ts=now,
            trades_15m=opens_15m,
            trades_60m=opens_60m,
            win_rate=wr,
            profit_factor=pf,
            equity_delta_15m_pct=eq15,
            action=action,
            notes=t.notes,
            tuning=t,
        )
        self._append_report(report)
        log.warning(
            "OPTIMIZER 15m | %s | cf_d=%+.3f agree_d=%+d gap=%.0fs",
            report.summary,
            t.confluence_delta,
            t.agreeing_delta,
            effective_entry_gap(self.settings, t),
        )
        return report


def run_standalone() -> int:
    """CLI entry for scheduled Task Scheduler runs (updates shared tuning file)."""
    from config import load_settings

    settings = load_settings()
    opt = ScalpOptimizer(settings.state_dir, settings)
    report = opt.maybe_optimize(0.0, force=True)
    if report:
        print(report.summary)
    else:
        print("optimizer skipped (disabled or throttled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_standalone())
