"""
PnL curve intelligence — measures how vertical the equity curve is and
steers every subsystem to keep it pointing up toward $95M.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_DAILY_PCT_IDEAL = 0.0  # filled dynamically from growth optimizer each tick


@dataclass
class PnlCurveState:
    verticality: float
    slope_1h_pct: float
    slope_6h_pct: float
    acceleration: float
    curve_phase: str
    peak_equity: float
    drawdown_from_peak_pct: float
    actual_daily_pct: float
    vs_required_daily_pct: float
    on_vertical_path: bool
    preserve_capital: bool
    harvest_eagerness: float
    entry_scale: float
    risk_scale: float


class PnlCurveEngine:
    """Tracks equity curve shape and outputs control scales for the bot."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.ticks_path = state_dir / "equity_ticks.jsonl"
        self.profit_path = state_dir / "profitability.json"
        self.curve_path = state_dir / "pnl_curve.json"
        self._peak = 0.0
        self._load_meta()

    def _load_meta(self) -> None:
        if self.curve_path.exists():
            try:
                raw = json.loads(self.curve_path.read_text(encoding="utf-8"))
                self._peak = float(raw.get("peak_equity", 0))
            except Exception:
                pass

    def reset_peak(self, equity: float) -> None:
        self._peak = max(float(equity), 0.01)

    def _save_meta(self, state: PnlCurveState, equity: float) -> None:
        if equity > self._peak:
            self._peak = equity
        self.curve_path.parent.mkdir(parents=True, exist_ok=True)
        self.curve_path.write_text(
            json.dumps(
                {
                    "peak_equity": self._peak,
                    "last_verticality": state.verticality,
                    "last_phase": state.curve_phase,
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_ticks(self, max_points: int = 500) -> list[tuple[float, float]]:
        if not self.ticks_path.exists():
            return []
        rows: list[tuple[float, float]] = []
        try:
            with self.ticks_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        rows.append((float(raw["ts"]), float(raw["equity"])))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        except Exception:
            return []
        return rows[-max_points:]

    def _slope_pct(self, points: list[tuple[float, float]], window_sec: float) -> float:
        if len(points) < 2:
            return 0.0
        now = points[-1][0]
        cutoff = now - window_sec
        old = [(t, e) for t, e in points if t <= cutoff]
        if not old:
            old = [points[0]]
        start_t, start_e = old[-1]
        end_t, end_e = points[-1]
        if start_e <= 0 or end_t <= start_t:
            return 0.0
        hours = (end_t - start_t) / 3600.0
        if hours <= 0:
            return 0.0
        total_ret = (end_e - start_e) / start_e
        # Annualize-ish to hourly then scale to "per day equivalent"
        hourly = total_ret / hours
        return hourly * 24.0 * 100.0

    def record_trade(self, symbol: str, net_pnl_usd: float, *, side: str = "", event: str = "close") -> None:
        self.profit_path.parent.mkdir(parents=True, exist_ok=True)
        raw: dict = {"trades": []}
        if self.profit_path.exists():
            try:
                raw = json.loads(self.profit_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {"trades": []}
        trades = raw.get("trades", [])
        trades.append(
            {
                "ts": time.time(),
                "symbol": symbol,
                "side": side,
                "net_pnl": round(net_pnl_usd, 6),
                "event": event,
            }
        )
        raw["trades"] = trades[-500:]
        self.profit_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    def update(self, equity: float, required_daily_pct: float, *, unrestricted: bool = False) -> PnlCurveState:
        ticks = self._load_ticks()
        if equity > 0:
            ticks.append((time.time(), equity))
        ticks = sorted(set(ticks), key=lambda x: x[0])[-500:]

        slope_1h = self._slope_pct(ticks, 3600)
        slope_6h = self._slope_pct(ticks, 21600)
        acceleration = slope_1h - slope_6h

        peak = max(self._peak, equity, max((e for _, e in ticks), default=0.0))
        dd_pct = 0.0
        if peak > 0 and equity > 0:
            dd_pct = max(0.0, (peak - equity) / peak * 100.0)

        # Verticality: strong positive slope + acceleration + low drawdown
        slope_score = _sigmoid(slope_1h / max(required_daily_pct, 0.5), k=1.2)
        accel_score = _sigmoid(0.5 + acceleration / 3.0, k=2.0)
        dd_score = max(0.0, 1.0 - dd_pct / 15.0)
        verticality = _clamp(slope_score * 0.5 + accel_score * 0.25 + dd_score * 0.25)

        if verticality >= 0.72 and slope_1h >= required_daily_pct * 0.85 and dd_pct < 4:
            phase = "vertical"
        elif slope_1h >= required_daily_pct * 0.5 and acceleration >= 0:
            phase = "climbing"
        elif slope_1h < -0.5 or dd_pct > 8:
            phase = "declining"
        else:
            phase = "flat"

        preserve = False if unrestricted else (
            phase in ("declining", "flat") or dd_pct > 6 or slope_1h < 0
        )

        if unrestricted:
            harvest_eagerness = 1.0
            entry_scale = 1.0
            risk_scale = 1.0
        elif phase == "vertical":
            harvest_eagerness = 0.85
            entry_scale = min(1.15, 0.95 + verticality * 0.2)
            risk_scale = min(1.1, 0.9 + verticality * 0.15)
        elif phase == "climbing":
            harvest_eagerness = 1.0
            entry_scale = 0.85 + verticality * 0.25
            risk_scale = 0.8 + verticality * 0.2
        elif phase == "flat":
            harvest_eagerness = 1.35
            entry_scale = 0.55
            risk_scale = 0.5
        else:
            harvest_eagerness = 1.55
            entry_scale = 0.35
            risk_scale = 0.3

        state = PnlCurveState(
            verticality=round(verticality, 4),
            slope_1h_pct=round(slope_1h, 3),
            slope_6h_pct=round(slope_6h, 3),
            acceleration=round(acceleration, 3),
            curve_phase=phase,
            peak_equity=round(peak, 4),
            drawdown_from_peak_pct=round(dd_pct, 2),
            actual_daily_pct=round(slope_1h, 3),
            vs_required_daily_pct=round(required_daily_pct, 3),
            on_vertical_path=phase == "vertical",
            preserve_capital=preserve,
            harvest_eagerness=round(harvest_eagerness, 3),
            entry_scale=round(entry_scale, 3),
            risk_scale=round(risk_scale, 3),
        )
        self._save_meta(state, equity)
        return state

    def format_report(self, state: PnlCurveState, equity: float) -> str:
        bar_len = 20
        filled = int(state.verticality * bar_len)
        bar = "#" * filled + "-" * (bar_len - filled)
        return (
            f"PNL CURVE | {state.curve_phase.upper()} | verticality [{bar}] {state.verticality:.0%}\n"
            f"  equity=${equity:.4f} peak=${state.peak_equity:.4f} dd={state.drawdown_from_peak_pct:.1f}%\n"
            f"  slope 1h={state.slope_1h_pct:+.2f}%/day-eq 6h={state.slope_6h_pct:+.2f}% accel={state.acceleration:+.2f}\n"
            f"  need {state.vs_required_daily_pct:.2f}%/day | harvest_eagerness={state.harvest_eagerness:.2f}x "
            f"entry_scale={state.entry_scale:.2f}x"
        )


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _sigmoid(x: float, k: float = 1.0) -> float:
    return 1.0 / (1.0 + math.exp(-k * x))
