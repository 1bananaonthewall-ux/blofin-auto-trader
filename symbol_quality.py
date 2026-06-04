"""Symbol quality memory for pruning weak symbols and penalizing bad execution."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class SymbolQualityStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "symbol_quality.json"
        self._rows: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._rows = raw
        except Exception:
            log.debug("symbol quality load failed", exc_info=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._rows, indent=2), encoding="utf-8")

    def _row(self, symbol: str) -> dict:
        row = self._rows.setdefault(
            symbol,
            {
                "score": 0.5,
                "wins": 0,
                "losses": 0,
                "opens": 0,
                "slippage_bps_ema": 0.0,
                "updated_ts": 0,
            },
        )
        row["updated_ts"] = int(time.time())
        return row

    def score(self, symbol: str) -> float:
        return float(self._row(symbol).get("score", 0.5))

    def allow(self, symbol: str, floor: float) -> bool:
        return self.score(symbol) >= floor

    def note_open(self, symbol: str) -> None:
        row = self._row(symbol)
        row["opens"] = int(row.get("opens", 0)) + 1

    def note_outcome(self, symbol: str, win: bool, *, roe_pct: float | None = None) -> None:
        row = self._row(symbol)
        if win:
            row["wins"] = int(row.get("wins", 0)) + 1
            row["score"] = min(1.0, float(row.get("score", 0.5)) + 0.02)
        else:
            row["losses"] = int(row.get("losses", 0)) + 1
            row["score"] = max(0.0, float(row.get("score", 0.5)) - 0.03)
        if roe_pct is not None:
            roe = float(roe_pct)
            ema = float(row.get("roe_ema") or roe)
            row["roe_ema"] = round(0.85 * ema + 0.15 * roe, 2)
            row["last_roe"] = round(roe, 2)
            if roe >= 10.0:
                row["score"] = min(1.0, float(row.get("score", 0.5)) + 0.03)
            elif roe >= 3.0:
                row["score"] = min(1.0, float(row.get("score", 0.5)) + 0.015)
            elif roe <= -20.0:
                row["score"] = max(0.0, float(row.get("score", 0.5)) - 0.05)
            elif roe <= -8.0:
                row["score"] = max(0.0, float(row.get("score", 0.5)) - 0.025)

    def note_run_quality(
        self,
        symbol: str,
        *,
        run_score: float,
        label: str = "mixed",
        is_runner: bool = False,
        is_choppy: bool = False,
    ) -> None:
        row = self._row(symbol)
        ema = float(row.get("run_score_ema", run_score))
        row["run_score_ema"] = round(0.80 * ema + 0.20 * float(run_score), 4)
        row["last_run_label"] = label
        row["is_runner"] = bool(is_runner)
        row["is_choppy"] = bool(is_choppy)
        if is_runner:
            row["score"] = min(1.0, float(row.get("score", 0.5)) + 0.02)
        elif is_choppy:
            row["score"] = max(0.0, float(row.get("score", 0.5)) - 0.04)

    def run_score(self, symbol: str) -> float:
        row = self._row(symbol)
        return float(row.get("run_score_ema", row.get("score", 0.5)))

    def skip_choppy_symbol(self, symbol: str, *, floor: float = 0.38) -> bool:
        row = self._row(symbol)
        if not row.get("last_run_label"):
            return False
        if row.get("is_choppy") and float(row.get("run_score_ema", 0.5)) < floor:
            return True
        return float(row.get("run_score_ema", 0.5)) < floor - 0.12 and row.get("last_run_label") == "choppy"

    def note_slippage(self, symbol: str, expected_entry: float, actual_entry: float) -> float | None:
        if expected_entry <= 0 or actual_entry <= 0:
            return None
        row = self._row(symbol)
        bps = abs((actual_entry - expected_entry) / expected_entry) * 10000.0
        ema = float(row.get("slippage_bps_ema", 0.0))
        ema = 0.9 * ema + 0.1 * bps if ema > 0 else bps
        row["slippage_bps_ema"] = round(ema, 4)
        if bps > 12:
            row["score"] = max(0.0, float(row.get("score", 0.5)) - min(0.05, (bps - 12) / 400.0))
        return bps

