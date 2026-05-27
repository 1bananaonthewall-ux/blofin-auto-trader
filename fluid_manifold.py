"""
Fluid control manifold — no discrete "modes".

Dozens of continuous signals (0–1) blend every tick into:
  - path_reliability: how safe it is to act toward $95M right now
  - action_intensity: how hard to press (scan, risk, leverage)
  - edge: signal quality / recent trading edge

Like a trader: when equity bleeds, intensity fades smoothly; when edge returns,
intensity rises without flipping a labeled state machine.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from mission_config import TARGET_CAPITAL_USD as TARGET_CAPITAL, target_date_ts

TARGET_DATE_TS = target_date_ts()

# Every factor feeds the objective; weights adapt from live outcomes.
MANIFOLD_FACTOR_NAMES: tuple[str, ...] = (
    "equity_health",
    "peak_distance",
    "trough_recovery",
    "velocity_5m",
    "velocity_15m",
    "velocity_30m",
    "momentum_up",
    "win_rate",
    "profit_factor",
    "loss_streak",
    "edge_quality",
    "schedule_pressure",
    "schedule_alignment",
    "days_urgency",
    "growth_boost",
    "margin_headroom",
    "margin_stress",
    "position_load",
    "ml_accuracy",
    "ml_long_edge",
    "ml_short_edge",
    "feedback_depth",
    "equity_stability",
    "vol_calm",
    "small_account_need",
    "confidence_discipline",
    "fee_efficiency",
    "path_momentum",
    "compound_health",
    "drawdown_softness",
    "recovery_slope",
    "pnl_verticality",
    "curve_slope",
    "curve_acceleration",
    "survival",
    "opportunity",
    "reliability",
    "action_intensity",
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _sigmoid(x: float, k: float = 4.0) -> float:
    return 1.0 / (1.0 + math.exp(-k * (x - 0.5)))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * _clamp01(t)


@dataclass
class FluidSnapshot:
    factors: dict[str, float]
    action_intensity: float
    path_reliability: float
    survival: float
    edge: float
    growth_pressure: float
    drawdown_pct: float
    allow_new_entries: bool
    force_retrain: bool
    drivers: list[str]


@dataclass
class ManifoldContext:
    equity: float
    free_margin: float
    open_count: int
    win_rate: float
    profit_factor: float
    consecutive_losses: int
    required_daily_pct: float
    on_track: bool
    days_remaining: int
    aggression_boost: float
    ml_val_accuracy: float = 0.55
    ml_long_precision: float = 0.5
    ml_short_precision: float = 0.5
    feedback_samples: int = 0
    pnl_verticality: float = 0.5
    curve_slope_norm: float = 0.5
    curve_acceleration_norm: float = 0.5


class FluidManifold:
    """Continuous control field toward $95M by Sept 1, 2027."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.weights_path = state_dir / "manifold_weights.json"
        self.state_path = state_dir / "fluid_state.json"
        self._samples: deque[tuple[float, float]] = deque(maxlen=360)
        self.peak_equity = 0.0
        self.trough_equity = 0.0
        self._ema_momentum = 0.0
        self._force_retrain = False
        self._last_retrain_flag_ts = 0.0
        self.weights: dict[str, float] = {n: 1.0 for n in MANIFOLD_FACTOR_NAMES}
        self._load()

    def _load(self) -> None:
        if self.weights_path.exists():
            try:
                raw = json.loads(self.weights_path.read_text(encoding="utf-8"))
                for k, v in raw.get("weights", {}).items():
                    if k in self.weights:
                        self.weights[k] = float(v)
            except Exception:
                pass
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.peak_equity = float(raw.get("peak_equity", 0))
                self.trough_equity = float(raw.get("trough_equity", 0))
                self._ema_momentum = float(raw.get("ema_momentum", 0))
                for item in raw.get("samples", [])[-180:]:
                    self._samples.append((float(item[0]), float(item[1])))
            except Exception:
                pass

    def reset_peaks(self, equity: float) -> None:
        eq = max(float(equity), 0.01)
        self.peak_equity = eq
        self.trough_equity = eq * 0.98
        self._force_retrain = False
        self._save()

    def clear_force_retrain(self) -> None:
        self._force_retrain = False

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "peak_equity": self.peak_equity,
                    "trough_equity": self.trough_equity,
                    "ema_momentum": round(self._ema_momentum, 6),
                    "samples": list(self._samples)[-120:],
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _drop_pct(self, now: float, window: float) -> float:
        cutoff = now - window
        old = [eq for ts, eq in self._samples if ts <= cutoff]
        if not old or not self._samples:
            return 0.0
        start, end = old[-1], self._samples[-1][1]
        if start <= 0:
            return 0.0
        return max(0.0, (start - end) / start * 100.0)

    def _rise_pct(self, now: float, window: float) -> float:
        cutoff = now - window
        old = [eq for ts, eq in self._samples if ts <= cutoff]
        if not old or not self._samples:
            return 0.0
        start, end = old[-1], self._samples[-1][1]
        if start <= 0:
            return 0.0
        return max(0.0, (end - start) / start * 100.0)

    def _weighted_mean(self, pairs: list[tuple[str, float]]) -> float:
        num, den = 0.0, 0.0
        for name, val in pairs:
            w = self.weights.get(name, 1.0)
            v = _clamp01(val)
            num += w * v
            den += w
        return num / den if den > 0 else 0.5

    def tick(self, ctx: ManifoldContext, *, unrestricted: bool = False) -> FluidSnapshot:
        now = time.time()
        eq = ctx.equity
        self._samples.append((now, eq))
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.trough_equity <= 0 or eq < self.trough_equity:
            self.trough_equity = eq

        drawdown_pct = 0.0
        if not unrestricted and self.peak_equity > 0:
            drawdown_pct = max(0.0, (self.peak_equity - eq) / self.peak_equity * 100.0)

        drop5 = self._drop_pct(now, 300)
        drop15 = self._drop_pct(now, 900)
        drop30 = self._drop_pct(now, 1800)
        rise15 = self._rise_pct(now, 900)

        tick_ret = 0.0
        if len(self._samples) >= 2:
            prev = self._samples[-2][1]
            if prev > 0:
                tick_ret = (eq - prev) / prev
        self._ema_momentum = 0.92 * self._ema_momentum + 0.08 * tick_ret

        recent = [e for _, e in list(self._samples)[-20:]]
        vol = 0.0
        if len(recent) >= 3:
            mean = sum(recent) / len(recent)
            vol = (sum((x - mean) ** 2 for x in recent) / len(recent)) ** 0.5 / max(mean, 1e-9)

        margin_ratio = ctx.free_margin / eq if eq > 0 else 0.0
        pf_norm = _clamp01((ctx.profit_factor - 0.5) / 2.0)
        wr = _clamp01(ctx.win_rate)
        loss_p = _clamp01(ctx.consecutive_losses / 6.0)
        req = min(15.0, ctx.required_daily_pct)
        sched_p = _clamp01(req / 8.0)
        sched_align = 1.0 if ctx.on_track else _clamp01(0.5 - sched_p * 0.3)
        urgency = _clamp01(1.0 - ctx.days_remaining / 500.0)
        fb = _clamp01(ctx.feedback_samples / 200.0)
        ml_acc = _clamp01((ctx.ml_val_accuracy - 0.5) / 0.3)
        ml_edge = _clamp01((ctx.ml_long_precision + ctx.ml_short_precision) / 2.0 - 0.4)

        f: dict[str, float] = {}
        f["equity_health"] = _clamp01(1.0 - drawdown_pct / 25.0)
        f["peak_distance"] = f["equity_health"]
        f["trough_recovery"] = _clamp01(
            (eq - self.trough_equity) / max(self.trough_equity, 1e-9) / 0.05
        ) if self.trough_equity > 0 else 0.5
        f["velocity_5m"] = _clamp01(1.0 - drop5 / 3.0)
        f["velocity_15m"] = _clamp01(1.0 - drop15 / 5.0)
        f["velocity_30m"] = _clamp01(1.0 - drop30 / 8.0)
        f["momentum_up"] = _clamp01(0.5 + self._ema_momentum * 80)
        f["win_rate"] = wr
        f["profit_factor"] = pf_norm
        f["loss_streak"] = 1.0 - loss_p
        f["edge_quality"] = self._weighted_mean([("win_rate", wr), ("profit_factor", pf_norm), ("ml_accuracy", ml_acc)])
        f["schedule_pressure"] = 1.0 - sched_p
        f["schedule_alignment"] = sched_align
        f["days_urgency"] = urgency
        f["growth_boost"] = _clamp01(ctx.aggression_boost / 2.0)
        f["margin_headroom"] = _clamp01(margin_ratio * 2.0)
        f["margin_stress"] = _clamp01(1.0 - margin_ratio)
        f["position_load"] = _clamp01(1.0 - ctx.open_count / 30.0)
        f["ml_accuracy"] = ml_acc
        f["ml_long_edge"] = _clamp01((ctx.ml_long_precision - 0.45) / 0.35)
        f["ml_short_edge"] = _clamp01((ctx.ml_short_precision - 0.45) / 0.35)
        f["feedback_depth"] = fb
        f["equity_stability"] = _clamp01(1.0 - vol * 50)
        f["vol_calm"] = f["equity_stability"]
        f["small_account_need"] = _clamp01(1.0 - eq / 500.0) if eq < 500 else 0.3
        f["confidence_discipline"] = _clamp01(0.6 + ml_edge * 0.4)
        f["fee_efficiency"] = f["edge_quality"]
        f["path_momentum"] = f["momentum_up"]
        f["compound_health"] = self._weighted_mean([
            ("equity_health", f["equity_health"]),
            ("schedule_alignment", sched_align),
            ("profit_factor", pf_norm),
        ])
        f["drawdown_softness"] = _sigmoid(1.0 - drawdown_pct / 12.0)
        f["recovery_slope"] = _clamp01(rise15 / 2.0)
        f["pnl_verticality"] = _clamp01(ctx.pnl_verticality)
        f["curve_slope"] = _clamp01(ctx.curve_slope_norm)
        f["curve_acceleration"] = _clamp01(ctx.curve_acceleration_norm)

        survival = self._weighted_mean([
            ("equity_health", f["equity_health"]),
            ("velocity_15m", f["velocity_15m"]),
            ("velocity_30m", f["velocity_30m"]),
            ("loss_streak", f["loss_streak"]),
            ("margin_headroom", f["margin_headroom"]),
            ("drawdown_softness", f["drawdown_softness"]),
            ("equity_stability", f["equity_stability"]),
        ])

        opportunity = self._weighted_mean([
            ("edge_quality", f["edge_quality"]),
            ("schedule_alignment", sched_align),
            ("ml_accuracy", ml_acc),
            ("recovery_slope", f["recovery_slope"]),
            ("pnl_verticality", f["pnl_verticality"]),
            ("curve_slope", f["curve_slope"]),
            ("curve_acceleration", f["curve_acceleration"]),
            ("growth_boost", f["growth_boost"]),
            ("feedback_depth", fb),
        ])

        growth_pressure = self._weighted_mean([
            ("schedule_pressure", 1.0 - f["schedule_pressure"]),
            ("days_urgency", urgency),
            ("small_account_need", f["small_account_need"]),
        ])

        # Single objective: reliable progress toward $95M
        path_reliability = survival * (0.55 + 0.45 * opportunity)
        path_reliability = _clamp01(path_reliability * (0.7 + 0.3 * f["compound_health"]))

        # Fluid intensity — no mode switch; vertical PnL curve amplifies press
        raw_intensity = (
            0.30 * opportunity
            + 0.25 * growth_pressure
            + 0.22 * path_reliability
            + 0.13 * f["pnl_verticality"]
            + 0.10 * f["recovery_slope"]
        )
        raw_intensity *= survival
        raw_intensity *= _clamp01(1.0 - loss_p * 0.85)
        if not unrestricted:
            if drawdown_pct > 6:
                damp = _sigmoid(1.0 - drawdown_pct / 18.0, k=3.0)
                raw_intensity *= damp
            if f["pnl_verticality"] < 0.35:
                raw_intensity *= _clamp01(0.5 + f["pnl_verticality"])
        action_intensity = _clamp01(max(0.42, raw_intensity) if unrestricted else raw_intensity)

        f["survival"] = survival
        f["opportunity"] = opportunity
        f["reliability"] = path_reliability
        f["action_intensity"] = action_intensity

        if unrestricted:
            allow = path_reliability >= 0.05 and survival >= 0.05
        else:
            allow = action_intensity >= 0.06 and path_reliability >= 0.12 and survival >= 0.15
            if drawdown_pct > 10 or drop30 > 4.5 or ctx.consecutive_losses >= 5:
                allow = action_intensity >= 0.22 and path_reliability >= 0.35
                if action_intensity < 0.08:
                    allow = False

        if not unrestricted:
            if drop30 > 6 or (ctx.profit_factor < 0.5 and ctx.consecutive_losses >= 4):
                now_ts = time.time()
                if now_ts - self._last_retrain_flag_ts >= 21600:
                    self._force_retrain = True
                    self._last_retrain_flag_ts = now_ts

        drivers = sorted(
            [(n, f[n]) for n in MANIFOLD_FACTOR_NAMES if n in f and n not in ("action_intensity", "reliability")],
            key=lambda x: x[1],
        )[:3]
        driver_strs = [f"{n}={v:.2f}" for n, v in drivers]
        limiting = sorted(
            [(n, f[n]) for n in ("survival", "edge_quality", "velocity_30m", "loss_streak") if n in f],
            key=lambda x: x[1],
        )[:2]
        lim_strs = [f"{n}={v:.2f}" for n, v in limiting]

        self._save()
        return FluidSnapshot(
            factors=f,
            action_intensity=round(action_intensity, 4),
            path_reliability=round(path_reliability, 4),
            survival=round(survival, 4),
            edge=round(opportunity, 4),
            growth_pressure=round(growth_pressure, 4),
            drawdown_pct=round(drawdown_pct, 2),
            allow_new_entries=allow,
            force_retrain=self._force_retrain,
            drivers=driver_strs + (["limit:" + lim_strs[0]] if lim_strs else []),
        )

    def consume_retrain_flag(self) -> bool:
        if self._force_retrain:
            self._force_retrain = False
            return True
        return False

    def nudge_weights(self, factor: str, direction: float) -> None:
        """Online learning: bump factors that preceded wins, trim after losses."""
        if factor not in self.weights:
            return
        self.weights[factor] = max(0.25, min(3.0, self.weights[factor] + direction * 0.03))
        self.weights_path.write_text(
            json.dumps({"weights": self.weights, "updated_at": time.time()}, indent=2),
            encoding="utf-8",
        )

    @property
    def parameter_count_estimate(self) -> int:
        """Rough count of tuned decision dimensions (manifold + ML ensemble)."""
        ml_trees = 200 + 200 + 1000  # rf + gb iter + lr coef scale
        return len(MANIFOLD_FACTOR_NAMES) * 8 + ml_trees + len(self.weights) * 4
