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

from optimizer_autocode import maybe_apply_autocode

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
    wins_last_hour: int = 0
    win_rate_recent: float = 0.0
    profit_factor_recent: float = 1.0
    avg_roe_recent: float = 0.0
    neg_roe_streak: int = 0
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


def _recent_trade_stats(state_dir: Path, since_ts: float) -> tuple[float, float, int, float, int]:
    """Win rate / PF / neg-ROE streak / avg ROE / close count from roe_learning store."""
    try:
        from roe_learning import get_roe_store

        store = get_roe_store(state_dir)
        recent_n = len(
            [
                r
                for r in (store._data.get("global", {}).get("recent") or [])
                if _parse_ts(r.get("ts", 0)) >= since_ts
            ]
        )
        if recent_n > 0:
            wr, pf, streak, avg_roe = store.recent_performance(max(60.0, time.time() - since_ts))
            return wr, pf, streak, avg_roe, recent_n
    except Exception:
        pass
    path = state_dir / "profitability.json"
    if not path.exists():
        return 0.5, 1.0, 0, 0.0, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trades = raw.get("trades", [])
        recent = [t for t in trades if _parse_ts(t.get("ts", t.get("closed_ts", 0))) >= since_ts]
        if not recent:
            recent = trades[-20:]
        if not recent:
            return 0.5, 1.0, 0, 0.0, 0
        roes = [float(t["roe_pct"]) for t in recent if t.get("roe_pct") is not None]
        if roes:
            wins = sum(1 for r in roes if r > 0)
            wr = wins / len(roes)
            pos = sum(r for r in roes if r > 0)
            neg = abs(sum(r for r in roes if r < 0))
            pf = (pos / neg) if neg > 0 else (2.0 if pos > 0 else 1.0)
            streak = 0
            for r in reversed(roes):
                if r < 0:
                    streak += 1
                else:
                    break
            return wr, pf, streak, round(sum(roes) / len(roes), 2), len(recent)
        wins = sum(1 for t in recent if float(t.get("net_pnl", 0)) > 0)
        wr = wins / len(recent)
        gp = sum(float(t["net_pnl"]) for t in recent if float(t.get("net_pnl", 0)) > 0)
        gl = abs(sum(float(t["net_pnl"]) for t in recent if float(t.get("net_pnl", 0)) < 0))
        pf = (gp / gl) if gl > 0 else (2.0 if gp > 0 else 1.0)
        return wr, pf, 0, 0.0, len(recent)
    except Exception:
        return 0.5, 1.0, 0, 0.0, 0


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


def _load_recent_reports(path: Path, limit: int) -> list[dict]:
    if not path.exists() or limit <= 0:
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(20, limit * 2) :]
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return []
    return rows[-limit:]


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


def _abundant_flow(settings: "Settings") -> bool:
    if not getattr(settings, "winner_only_mode", False):
        return False
    if getattr(settings, "entries_never_pause", False):
        return True
    try:
        from account_guard import universe_fill_active

        return universe_fill_active(settings)
    except Exception:
        return False


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
    if getattr(settings, "hourly_3r_winner_mode", False):
        quality_first = False
    cf_d = t.confluence_delta if not quality_first else max(0.0, t.confluence_delta)
    agree_d = t.agreeing_delta if not quality_first else max(0, t.agreeing_delta)
    ml_d = t.ml_conf_delta if not quality_first else max(0.0, t.ml_conf_delta)
    score_d = t.min_score_delta if not quality_first else max(0.0, t.min_score_delta)

    abundant = _abundant_flow(settings)
    cf_floor = 0.48 if abundant else 0.52
    agree_floor = 3 if abundant else 4
    ml_floor = 0.60 if abundant else 0.68
    score_floor = 0.44 if abundant else 0.48

    cf = max(cf_floor, min(0.72, base_cf + cf_d))
    agree = max(agree_floor, min(9, base_agree + agree_d))
    ml = max(ml_floor, min(0.88, base_ml + ml_d))
    vol = max(0.18 if abundant else 0.95, min(1.8, base_vol + t.volume_delta))
    score = max(score_floor, min(0.72, base_score + score_d))
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
    from account_guard import universe_fill_active

    t = tuning or get_active_tuning()
    base = settings.scalp_entry_gap_seconds
    if t.entry_gap_seconds is not None:
        gap = t.entry_gap_seconds
    else:
        gap = base
    if universe_fill_active(settings):
        return max(2.0, min(12.0, gap))
    return max(15.0, min(75.0, gap))


def effective_cooldown_minutes(settings: "Settings", tuning: ScalpTuning | None = None) -> float:
    from account_guard import universe_fill_active

    t = tuning or get_active_tuning()
    base = float(settings.scalp_cooldown_minutes)
    if t.symbol_cooldown_minutes is not None:
        cd = t.symbol_cooldown_minutes
    else:
        cd = base
    if universe_fill_active(settings):
        return max(1.0, min(3.0, cd * 0.4))
    return max(2.0, min(15.0, cd))


class ScalpOptimizer:
    def __init__(self, state_dir: Path, settings: "Settings") -> None:
        self.state_dir = state_dir
        self.settings = settings
        self.tuning_path = state_dir / TUNING_PATH_NAME
        self.report_path = state_dir / REPORT_PATH_NAME
        self.interval = float(getattr(settings, "optimizer_interval_seconds", 900))
        self.target_min_tph = int(getattr(settings, "optimizer_target_min_tph", 3))
        from account_guard import effective_hourly_tph_cap

        self.target_max_tph = effective_hourly_tph_cap(settings)
        quality_first = getattr(settings, "optimizer_quality_first", True)
        if getattr(settings, "quality_pick_mode", True):
            quality_first = True
        elif getattr(settings, "hourly_3r_winner_mode", False):
            quality_first = False
        self._throughput_mode = not quality_first
        self._last_run = 0.0
        self.tuning = ScalpTuning()
        self._load()

    def _flow_reward(self, row: dict, target_tph: int) -> float:
        tph = float(row.get("trades_60m", 0) or 0)
        wr = float(row.get("win_rate", 0.5) or 0.5)
        pf = float(row.get("profit_factor", 1.0) or 1.0)
        eq15 = float(row.get("equity_delta_15m_pct", 0.0) or 0.0)
        r = min(1.8, tph / max(1.0, float(target_tph)))
        # Keep quality in the objective so "more trades" does not mean pure churn.
        r += max(-0.8, min(0.8, eq15 / 2.0))
        if wr < 0.42:
            r -= 0.35
        if pf < 0.90:
            r -= 0.35
        return r

    def _learn_flow_adjustment(self) -> tuple[float, int, float, float, str]:
        if not getattr(self.settings, "optimizer_flow_learning_enabled", True):
            return 0.0, 0, 0.0, 0.0, "flow_off"
        lookback = int(getattr(self.settings, "optimizer_flow_lookback_reports", 80))
        target_tph = int(
            getattr(
                self.settings,
                "optimizer_flow_target_tph",
                max(self.target_min_tph, 3),
            )
        )
        rows = _load_recent_reports(self.report_path, lookback)
        if len(rows) < 8:
            return 0.0, 0, 0.0, 0.0, "flow_coldstart"

        # Group reward by action and use the best-performing action family.
        by_action: dict[str, list[float]] = {}
        for row in rows:
            act = str(row.get("action") or "hold")
            by_action.setdefault(act, []).append(self._flow_reward(row, target_tph))
        if not by_action:
            return 0.0, 0, 0.0, 0.0, "flow_empty"

        action_scores = {
            a: (sum(vals) / len(vals), len(vals))
            for a, vals in by_action.items()
            if vals
        }
        best_action, (best_score, n) = max(action_scores.items(), key=lambda kv: kv[1][0])
        if n < 3:
            return 0.0, 0, 0.0, 0.0, f"flow_weak:{best_action}"

        # Translate learned action into bounded deltas.
        if best_action in {"loosen_throughput", "accelerate_hot", "pace_up_quality"}:
            return -0.01, -1, -0.008, -0.012, f"flow_boost:{best_action}:{best_score:.2f}"
        if best_action in {"tighten_quality", "slow_overtrade"}:
            return +0.01, +1, +0.008, +0.010, f"flow_tighten:{best_action}:{best_score:.2f}"
        return 0.0, 0, 0.0, 0.0, f"flow_hold:{best_action}:{best_score:.2f}"

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
        interval = self.interval
        if self.tuning.trades_last_hour < max(3, self.target_min_tph):
            interval = min(interval, 300.0)
        if not force and (now - self._last_run) < interval:
            return None
        if not getattr(self.settings, "optimizer_enabled", True):
            return None

        self._last_run = now
        journal = self.state_dir / "trades.jsonl"
        t15 = now - 900
        t60 = now - 3600

        opens_15m = _count_journal_opens(journal, t15)
        opens_60m = _count_journal_opens(journal, t60)
        from hourly_3r import count_wins_since

        wins_60m = count_wins_since(self.state_dir, t60)
        wr, pf, neg_streak, avg_roe, closed_n = _recent_trade_stats(self.state_dir, t60)
        if closed_n == 0:
            wr, pf = win_rate, profit_factor
        eq15 = _equity_delta_pct(self.state_dir, 900)

        t = self.tuning
        t.trades_last_hour = opens_60m
        t.wins_last_hour = wins_60m
        t.win_rate_recent = wr
        t.profit_factor_recent = pf
        t.avg_roe_recent = avg_roe
        t.neg_roe_streak = neg_streak
        t.equity_delta_15m_pct = eq15
        action = "hold"
        notes: list[str] = []

        base_gap = self.settings.scalp_entry_gap_seconds
        base_cd = float(self.settings.scalp_cooldown_minutes)

        quality_first = getattr(self.settings, "quality_pick_mode", True) or (
            getattr(self.settings, "optimizer_quality_first", True) and not self._throughput_mode
        )
        flow_cf, flow_agree, flow_ml, flow_score, flow_note = self._learn_flow_adjustment()

        from hourly_3r import (
            hourly_3r_active,
            target_min_opens_per_hour,
            target_wins_per_hour,
        )

        hourly = hourly_3r_active(self.settings)
        min_wins = target_wins_per_hour(self.settings)
        min_opens = (
            target_min_opens_per_hour(self.settings)
            if hourly
            else self.target_min_tph
        )
        wins_starved = hourly and wins_60m < min_wins
        opens_starved = opens_60m < min_opens
        starved = wins_starved or opens_starved

        # Throughput: starved but not bleeding → pace up (quality-first never loosens gates)
        pace_only = hourly or getattr(self.settings, "tpsl_only_pacing", False)
        roe_tighten = neg_streak >= 4 or avg_roe <= -10.0
        if roe_tighten and starved and _abundant_flow(self.settings):
            roe_tighten = neg_streak >= 7 or avg_roe <= -18.0

        if starved and eq15 > -2.5:
            if not pace_only:
                gap = (t.entry_gap_seconds or base_gap) - 4.0
                t.entry_gap_seconds = max(8.0, gap)
                cd = (t.symbol_cooldown_minutes or base_cd) - 1.0
                t.symbol_cooldown_minutes = max(2.0, cd)
            if quality_first:
                action = "pace_up_quality"
                notes.append(f"starved<{self.target_min_tph}/hr pace-only (gates fixed)")
            else:
                t.confluence_delta = max(-0.10, t.confluence_delta - 0.018)
                t.agreeing_delta = max(-3, t.agreeing_delta - 1)
                t.ml_conf_delta = max(-0.08, t.ml_conf_delta - 0.015)
                t.min_score_delta = max(-0.08, t.min_score_delta - 0.025)
                action = "loosen_throughput"
                if wins_starved:
                    notes.append(f"wins<{min_wins}/hr ({wins_60m})")
                if opens_starved:
                    notes.append(f"opens<{min_opens}/hr ({opens_60m})")

        # ROE bleed → tighten (winner-flow starvation keeps loosening priority)
        elif roe_tighten:
            t.confluence_delta = min(0.10, t.confluence_delta + 0.025)
            t.agreeing_delta = min(2, t.agreeing_delta + 1)
            t.ml_conf_delta = min(0.08, t.ml_conf_delta + 0.02)
            t.min_score_delta = min(0.08, t.min_score_delta + 0.025)
            if not pace_only:
                gap = (t.entry_gap_seconds or base_gap) + 4.0
                t.entry_gap_seconds = min(65.0, gap)
            action = "tighten_roe"
            notes.append(f"roe_streak={neg_streak} avg_roe={avg_roe:+.1f}%")

        # Quality bleed → tighten (still trading, just pickier)
        elif wr < 0.40 or pf < 0.85 or eq15 < -3.0:
            t.confluence_delta = min(0.08, t.confluence_delta + 0.02)
            t.agreeing_delta = min(2, t.agreeing_delta + 1)
            t.ml_conf_delta = min(0.06, t.ml_conf_delta + 0.015)
            t.min_score_delta = min(0.06, t.min_score_delta + 0.02)
            if not pace_only:
                gap = (t.entry_gap_seconds or base_gap) + 3.0
                t.entry_gap_seconds = min(60.0, gap)
            action = "tighten_quality"
            notes.append(f"wr={wr:.0%} pf={pf:.2f}")

        # Winning hot → slight loosen to capture more A+ hours (disabled in quality-pick mode)
        elif (
            not getattr(self.settings, "quality_pick_mode", True)
            and wr >= 0.52
            and pf >= 1.15
            and opens_60m < self.target_max_tph
        ):
            t.confluence_delta = max(-0.06, t.confluence_delta - 0.008)
            t.min_score_delta = max(-0.04, t.min_score_delta - 0.01)
            if not pace_only:
                gap = (t.entry_gap_seconds or base_gap) - 2.0
                t.entry_gap_seconds = max(18.0, gap)
            action = "accelerate_hot"
            notes.append("winning streak")

        # Over-trading weak → tighten pace only (disabled in universe-fill mode)
        elif (
            opens_60m > self.target_max_tph
            and wr < 0.48
            and not getattr(self.settings, "universe_fill_mode", False)
            and not getattr(self.settings, "trade_all_symbols", False)
        ):
            if not pace_only:
                gap = (t.entry_gap_seconds or base_gap) + 5.0
                t.entry_gap_seconds = min(65.0, gap)
            t.confluence_delta = min(0.06, t.confluence_delta + 0.01)
            action = "slow_overtrade"
            notes.append(f"overtrading>{self.target_max_tph}/hr")

        # Learned flow bias: adapt from what historically improved tph without quality collapse.
        if (
            not getattr(self.settings, "quality_pick_mode", True)
            and flow_note
            and not flow_note.startswith("flow_coldstart")
        ):
            t.confluence_delta = max(-0.10, min(0.10, t.confluence_delta + flow_cf))
            t.agreeing_delta = max(-3, min(3, t.agreeing_delta + flow_agree))
            t.ml_conf_delta = max(-0.08, min(0.08, t.ml_conf_delta + flow_ml))
            t.min_score_delta = max(-0.08, min(0.08, t.min_score_delta + flow_score))
            notes.append(flow_note)

        if not ml_ready and opens_60m < 1:
            notes.append("ML not ready — run train_model.py for better picks")

        t.action = action
        t.notes = "; ".join(notes) if notes else "steady"
        self._save()
        auto_mode = maybe_apply_autocode(
            self.state_dir,
            enabled=getattr(self.settings, "optimizer_autocode_enabled", True)
            and not getattr(self.settings, "quality_pick_mode", True),
            action=action,
            win_rate=wr,
            profit_factor=pf,
            equity_delta_15m_pct=eq15,
            trades_last_hour=opens_60m,
            cooldown_sec=int(getattr(self.settings, "optimizer_autocode_cooldown_seconds", 900)),
        )
        if auto_mode not in {"disabled", "unchanged"}:
            t.notes = (t.notes + f"; autocode={auto_mode}").strip("; ")

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


_micro_last_nudge = 0.0


def micro_tune_for_flow(
    state_dir: Path,
    settings: "Settings",
    *,
    ranked_count: int,
    top_conviction: float,
    top_tier: str,
    top_winner_score: float,
    mission_floor: float,
    cooldown_sec: float = 90.0,
) -> tuple[float, str]:
    """
    Fast in-cycle nudge when winner-passed setups exist but conviction blocks open.
    Returns (adjusted_mission_floor, note).
    """
    global _micro_last_nudge, _active
    if getattr(settings, "quality_pick_mode", True):
        return mission_floor, ""
    if ranked_count <= 0 or top_tier not in {"good", "elite", "apex"}:
        return mission_floor, ""
    if top_winner_score < 0.48:
        return mission_floor, ""

    now = time.time()
    if top_conviction >= mission_floor - 0.005:
        return mission_floor, ""

    opt = ScalpOptimizer(state_dir, settings)
    t = opt.tuning
    note = ""
    if now - _micro_last_nudge >= cooldown_sec:
        _micro_last_nudge = now
        t.confluence_delta = max(-0.12, t.confluence_delta - 0.006)
        t.agreeing_delta = max(-3, t.agreeing_delta - 1 if ranked_count >= 8 else 0)
        t.ml_conf_delta = max(-0.10, t.ml_conf_delta - 0.004)
        t.min_score_delta = max(-0.10, t.min_score_delta - 0.006)
        t.notes = (t.notes + "; micro_flow").strip("; ")
        opt._save()
        note = "micro_tune"

    adaptive = max(0.38, min(mission_floor, top_conviction + 0.008))
    if adaptive < mission_floor:
        return adaptive, note or "adaptive_conv"
    return mission_floor, note


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
