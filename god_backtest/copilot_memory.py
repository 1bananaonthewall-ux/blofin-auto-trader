"""Walk-forward cortex memory for copilot A/B backtests (no lookahead)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CortexMemory:
    """Mirrors live cortex / trade_lessons learning from closed trades only."""

    sym_side_wins: dict[tuple[str, str], int] = field(default_factory=dict)
    sym_side_losses: dict[tuple[str, str], int] = field(default_factory=dict)
    sym_wins: dict[str, int] = field(default_factory=dict)
    sym_losses: dict[str, int] = field(default_factory=dict)
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=48))
    total_closes: int = 0
    vetoes: int = 0
    approvals: int = 0

    def record(self, inst_id: str, side: str, result: str) -> None:
        key = (inst_id, side)
        if result == "win":
            self.sym_side_wins[key] = self.sym_side_wins.get(key, 0) + 1
            self.sym_wins[inst_id] = self.sym_wins.get(inst_id, 0) + 1
        else:
            self.sym_side_losses[key] = self.sym_side_losses.get(key, 0) + 1
            self.sym_losses[inst_id] = self.sym_losses.get(inst_id, 0) + 1
        self.recent.append({"sym": inst_id, "side": side, "result": result})
        self.total_closes += 1

    def _wr(self, inst_id: str, side: str) -> tuple[float | None, int]:
        w = self.sym_side_wins.get((inst_id, side), 0)
        l = self.sym_side_losses.get((inst_id, side), 0)
        n = w + l
        if n < 4:
            return None, n
        return w / n, n

    def _sym_loss_streak(self, inst_id: str) -> int:
        streak = 0
        for row in reversed(self.recent):
            if row["sym"] != inst_id:
                continue
            if row["result"] == "loss":
                streak += 1
            else:
                break
        return streak

    def _recent_wr(self, window: int = 20) -> float | None:
        if not self.recent:
            return None
        tail = list(self.recent)[-window:]
        if len(tail) < 6:
            return None
        return sum(1 for r in tail if r["result"] == "win") / len(tail)

    def copilot_approve(self, inst_id: str, side: str, dec: Any, *, strict: bool = True) -> tuple[bool, str]:
        """
        Approve/veto a finalist setup using only past simulated closes.
        Early phase is picky (untrained); later uses symbol-side stats like live cortex.
        """
        conf = float(getattr(dec, "model_confidence", 0) or (getattr(dec, "score", 0) / 100.0))
        cf = float(getattr(dec, "confluence_score", 0) or conf)

        if self.total_closes < 12:
            if cf < 0.49 and conf < 0.57:
                self.vetoes += 1
                return False, "bootstrap_low_edge"
            if self._sym_loss_streak(inst_id) >= 2:
                self.vetoes += 1
                return False, "bootstrap_loss_streak"
            self.approvals += 1
            return True, "bootstrap_pass"

        wr, n = self._wr(inst_id, side)
        if wr is not None and wr < 0.34:
            self.vetoes += 1
            return False, f"side_cold_wr={wr:.0%}n={n}"

        sym_w = self.sym_wins.get(inst_id, 0)
        sym_l = self.sym_losses.get(inst_id, 0)
        sym_n = sym_w + sym_l
        if sym_n >= 5 and sym_w / sym_n < 0.32:
            self.vetoes += 1
            return False, f"symbol_toxic_wr={sym_w/sym_n:.0%}"

        if self._sym_loss_streak(inst_id) >= 3:
            self.vetoes += 1
            return False, "loss_streak_3"

        r_wr = self._recent_wr()
        if r_wr is not None and r_wr < 0.40:
            if cf < 0.51 or conf < 0.59:
                self.vetoes += 1
                return False, f"global_tighten_rwr={r_wr:.0%}"

        if strict and wr is not None and wr < 0.45 and cf < 0.50:
            self.vetoes += 1
            return False, f"strict_marginal_wr={wr:.0%}"

        self.approvals += 1
        return True, "approved"
