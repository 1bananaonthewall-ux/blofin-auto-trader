"""Track real-trade outcomes (win / loss) and their entry feature vectors
for feedback into retraining."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class TradeOutcomeTracker:
    """Records entry-time ML features when a position opens and, upon close,
    labels the outcome as win (hit TP) or loss (hit SL) so the model can
    learn from its own live decisions."""

    def __init__(self, state_dir: Path, max_samples: int = 500) -> None:
        self.path = state_dir / "trade_outcomes.jsonl"
        self.max_samples = max_samples
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Recording entry features when a position is opened
    # ------------------------------------------------------------------
    def record_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        take_price: float,
        feature_vector: list[float],
        signal_score: float,
        timestamp_ms: int | None = None,
    ) -> None:
        """Store the feature vector observed at entry time, plus metadata,
        so we can later label outcome (win / loss)."""
        record = {
            "event": "entry",
            "symbol": symbol,
            "side": side,
            "entry_price": round(entry_price, 8),
            "stop_price": round(stop_price, 8),
            "take_price": round(take_price, 8),
            "feature_vector": [round(v, 8) for v in feature_vector],
            "signal_score": signal_score,
            "ts": int(timestamp_ms or time.time() * 1000),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        log.debug("recorded entry for %s %s", symbol, side)

    # ------------------------------------------------------------------
    # Label outcome when a position closes
    # ------------------------------------------------------------------
    def record_close(
        self,
        symbol: str,
        side: str,
        close_price: float,
        entry_price: float,
        stop_price: float,
        take_price: float,
        reason: str = "unknown",
    ) -> dict[str, Any] | None:
        """Find the most recent unmatched entry for (symbol, side) and label
        outcome.  Returns the labelled record or None."""
        entry = self._find_entry(symbol, side)
        if entry is None:
            log.warning("no matching entry for close %s %s", symbol, side)
            return None

        # Determine outcome based on price vs SL/TP
        if side == "long":
            if close_price >= take_price:
                outcome = "win"
            elif close_price <= stop_price:
                outcome = "loss"
            else:
                outcome = "neutral"
        else:  # short
            if close_price <= take_price:
                outcome = "win"
            elif close_price >= stop_price:
                outcome = "loss"
            else:
                outcome = "neutral"

        label = 0 if side == "long" else 1  # same as training: 0=long, 1=short
        win_flag = 1 if outcome == "win" else 0

        record: dict[str, Any] = {
            "event": "outcome",
            "symbol": symbol,
            "side": side,
            "outcome": outcome,
            "label": label,
            "win": win_flag,
            "entry_price": entry.get("entry_price"),
            "close_price": round(close_price, 8),
            "stop_price": stop_price,
            "take_price": take_price,
            "entry_ts": entry.get("ts"),
            "close_ts": int(time.time() * 1000),
            "feature_vector": entry.get("feature_vector"),
            "signal_score": entry.get("signal_score", 0),
            "reason": reason,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        log.info(
            "outcome %s %s %s entry=%.4f close=%.4f sl=%.4f tp=%.4f",
            symbol,
            side,
            outcome,
            entry["entry_price"],
            close_price,
            stop_price,
            take_price,
        )
        return record

    # ------------------------------------------------------------------
    # Load labelled outcomes for merging into training
    # ------------------------------------------------------------------
    def load_labelled_samples(
        self, max_samples: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (X_feedback, y_feedback) from recorded outcomes for
        inclusion in training."""
        limit = max_samples or self.max_samples
        rows: list[dict[str, Any]] = []
        if not self.path.exists():
            return np.empty((0, 0)), np.empty((0,))

        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == "outcome" and row.get("outcome") != "neutral":
                    rows.append(row)

        # Keep most recent samples up to limit
        rows = rows[-limit:]

        if not rows:
            return np.empty((0, 0)), np.empty((0,))

        X_list: list[np.ndarray] = []
        y_list: list[int] = []

        for row in rows:
            fv = row.get("feature_vector")
            if not fv or not isinstance(fv, list):
                continue
            X_list.append(np.array(fv, dtype=np.float64))
            # label: 0 = long, 1 = short  (same as training)
            y_list.append(int(row["label"]))

        if not X_list:
            return np.empty((0, 0)), np.empty((0,))

        X = np.vstack(X_list)
        y = np.array(y_list, dtype=np.int64)
        log.info("loaded %d real-feedback samples for retraining", len(y))
        return X, y

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _find_entry(self, symbol: str, side: str) -> dict[str, Any] | None:
        """Scan outcomes.jsonl backwards to find the most recent 'entry'
        record for (symbol, side) that has no matching 'outcome' record
        after it.  We detect matching by scanning forward from an entry
        and checking if any 'outcome' with the same symbol/side exists."""
        if not self.path.exists():
            return None

        with self.path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()

        # Find all entry and outcome indices for this symbol/side
        entry_indices: list[int] = []
        outcome_indices: set[int] = set()

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = row.get("event")
            sym = row.get("symbol")
            sd = row.get("side")
            if sym != symbol or sd != side:
                continue
            if evt == "entry":
                entry_indices.append(i)
            elif evt == "outcome":
                outcome_indices.add(i)

        # Find the latest entry that has no outcome after it
        for ei in reversed(entry_indices):
            # Check no outcome index > ei
            has_outcome = any(oi > ei for oi in outcome_indices)
            if not has_outcome:
                return json.loads(lines[ei])
        return None