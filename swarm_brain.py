"""
Local Moon-style swarm — six free agents, no LLM API bills.

Mirrors moon-dev-ai-agents swarm/risk/strategy patterns using existing
confluence, ML, winner, pick, and playbook doctrine votes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playbook_loader import Playbook, load_playbook
from strategy import Signal

if TYPE_CHECKING:
    from config import Settings
    from pick_engine import MLContext
    from strategy import StrategyDecision
    from ta_confluence import ConfluenceFrame

log = logging.getLogger(__name__)

_brain: "MoonSwarmBrain | None" = None


@dataclass(frozen=True)
class SwarmVote:
    agent: str
    vote: int
    weight: float
    note: str


@dataclass(frozen=True)
class SwarmConsensus:
    ok: bool
    side: str
    confidence: float
    majority: float
    votes: tuple[SwarmVote, ...]
    reason: str

    @property
    def summary(self) -> str:
        parts = [f"{v.agent}={v.vote:+d}" for v in self.votes]
        return f"{self.side} {self.confidence:.0%} ({', '.join(parts)})"


def get_swarm_brain() -> "MoonSwarmBrain":
    global _brain
    if _brain is None:
        _brain = MoonSwarmBrain()
    return _brain


class MoonSwarmBrain:
    def __init__(self, playbook: Playbook | None = None) -> None:
        self.playbook = playbook or load_playbook()

    def _doctrine(self, key: str) -> dict:
        d = self.playbook.doctrines.get(key)
        return dict(d.params) if d else {}

    def _target_vote(self, signal: Signal) -> int:
        if signal == Signal.LONG:
            return 1
        if signal == Signal.SHORT:
            return -1
        return 0

    def consensus(
        self,
        decision: "StrategyDecision",
        cf: "ConfluenceFrame",
        settings: "Settings",
        *,
        ml_ctx: "MLContext | None" = None,
        winner_tier: str = "",
        pick_score: float = 0.0,
        starved: bool = False,
        overheating: bool = False,
    ) -> SwarmConsensus:
        target = self._target_vote(decision.signal)
        if target == 0:
            return SwarmConsensus(False, "flat", 0.0, 0.0, (), "flat signal")

        votes: list[SwarmVote] = []

        # 1 — Confluence agent
        cf_score = float(getattr(cf, "confluence_score", 0.0) or 0.0)
        agree = len(getattr(cf, "agreeing", []) or [])
        oppose = len(getattr(cf, "opposing", []) or [])
        cf_vote = target if cf_score >= 0.55 and agree > oppose else 0
        votes.append(SwarmVote("confluence", cf_vote, 1.2, f"cf={cf_score:.2f} {agree}/{oppose}"))

        # 2 — ML agent
        ml_vote = 0
        ml_note = "ml off"
        if ml_ctx and ml_ctx.ready and ml_ctx.signal in (Signal.LONG, Signal.SHORT):
            ml_side = self._target_vote(ml_ctx.signal)
            conf = float(ml_ctx.confidence or 0.0)
            if conf >= settings.ml_min_confidence - 0.08:
                ml_vote = ml_side if ml_side == target else (0 if conf < 0.62 else -target)
            ml_note = f"ml={ml_ctx.signal.value} {conf:.2f}"
        votes.append(SwarmVote("ml", ml_vote, 1.0, ml_note))

        # 3 — Winner / strategy agent
        tier = winner_tier or getattr(decision, "winner_tier", "")
        ws = float(getattr(decision, "winner_score", 0.0) or 0.0)
        strat = self._doctrine("strategy_last_say")
        min_rr = float(strat.get("min_rr", 2.8))
        rr = float(getattr(decision, "take_pct", 0) / max(getattr(decision, "stop_pct", 1e-9), 1e-9))
        w_vote = target if tier in ("apex", "elite") and ws >= settings.winner_elite_score and rr >= min_rr else 0
        votes.append(SwarmVote("strategy", w_vote, 1.15, f"tier={tier} ws={ws:.2f} rr={rr:.1f}"))

        # 4 — Pick / momentum agent
        ps = pick_score or float(getattr(decision, "pick_score", 0.0) or 0.0)
        p_vote = target if ps >= settings.pick_min_score else 0
        votes.append(SwarmVote("pick", p_vote, 1.0, f"pick={ps:.2f}"))

        # 5 — Risk agent
        risk = self._doctrine("risk_agent")
        max_opp = float(risk.get("max_opposition_ratio", 0.45))
        opp_ratio = oppose / max(agree + oppose, 1)
        funding = float(getattr(decision, "funding_rate", 0.0) or 0.0)
        funding_bad = (
            risk.get("funding_veto", True)
            and ((target > 0 and funding > settings.max_funding_long) or (target < 0 and funding < settings.min_funding_short))
        )
        r_vote = 0 if opp_ratio > max_opp or funding_bad else target
        votes.append(SwarmVote("risk", r_vote, 1.25, f"opp={opp_ratio:.2f} fund={funding:.4f}"))

        # 6 — Volume + playbook agent
        vol = self._doctrine("volume_early")
        min_vol = float(vol.get("min_volume_ratio", settings.min_volume_ratio))
        vr = float(getattr(decision, "volume_ratio", 0.0) or 0.0)
        v_vote = target if vr >= min_vol else 0
        votes.append(SwarmVote("volume", v_vote, 0.9, f"vol={vr:.2f}"))

        weighted = sum(v.vote * v.weight for v in votes)
        total_w = sum(abs(v.weight) for v in votes) or 1.0
        norm = weighted / total_w
        side = "long" if norm > 0.15 else ("short" if norm < -0.15 else "flat")

        aligned = sum(1 for v in votes if v.vote == target)
        majority = aligned / len(votes)
        confidence = min(1.0, abs(norm) * 0.5 + majority * 0.5)

        dual = self._doctrine("dual_mode")
        if overheating:
            need = float(dual.get("hot_consensus", 0.75))
        elif starved:
            need = float(dual.get("starved_consensus", 0.50))
        else:
            need = float(dual.get("normal_consensus", 0.67))
        swarm_doc = self._doctrine("swarm_consensus")
        doc_floor = float(swarm_doc.get("min_majority", 0.67))
        if starved:
            need = min(need, doc_floor, 0.52)
        else:
            need = max(need, doc_floor)
        min_votes = int(swarm_doc.get("min_votes", 4))
        if starved:
            min_votes = max(3, min_votes - 1)
        if getattr(decision, "confluence_zone", "") == "llm":
            need = min(need, 0.50)
            min_votes = max(3, min_votes - 1)

        ok = side == decision.signal.value and aligned >= min_votes and majority >= need
        reason = (
            f"swarm {aligned}/{len(votes)} align {majority:.0%}>={need:.0%}"
            if ok
            else f"swarm reject align {aligned}/{len(votes)} {majority:.0%} need {need:.0%} side={side}"
        )
        if ok:
            log.info("SWARM pass %s | %s", getattr(decision, "symbol", "?"), reason)

        return SwarmConsensus(
            ok=ok,
            side=side,
            confidence=round(confidence, 4),
            majority=round(majority, 4),
            votes=tuple(votes),
            reason=reason,
        )
