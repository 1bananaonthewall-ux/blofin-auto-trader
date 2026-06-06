"""
Winner-only entry selection — BloHunter-style quality bar for 3R scalps.

Filters FOR edge at entry time. Does not pause the bot or block after losses;
weak setups simply never fire. Fast 3R exits + high leverage stay unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from strategy import Signal, StrategyDecision

from ml.edge_gate import rolling_expectancy
from pick_engine import MLContext
from scalp_optimizer import EffectiveWinnerThresholds, effective_winner_thresholds, get_active_tuning
from hourly_3r import hourly_3r_active, is_entry_starved

if TYPE_CHECKING:
    from config import Settings
    from ta_confluence import ConfluenceResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WinnerVerdict:
    ok: bool
    tier: str  # apex | elite | good | reject
    score: float
    reason: str


def _side_ok(decision: StrategyDecision, cf: "ConfluenceResult") -> bool:
    d = decision.signal
    if d == Signal.LONG:
        return cf.votes_long >= cf.votes_short
    if d == Signal.SHORT:
        return cf.votes_short >= cf.votes_long
    return False


def evaluate_winner(
    decision: StrategyDecision,
    cf: "ConfluenceResult",
    settings: "Settings",
    *,
    symbol: str = "",
    ml_decision: StrategyDecision | None = None,
    ml_ready: bool = False,
    ml_ctx: MLContext | None = None,
) -> WinnerVerdict:
    """Return whether this setup clears the winner-only bar."""
    if not settings.winner_only_mode:
        return WinnerVerdict(True, "good", 0.7, "winner_only off")

    side = decision.signal
    if side == Signal.FLAT:
        return WinnerVerdict(False, "reject", 0.0, "flat")

    if symbol:
        try:
            from trade_lessons import entry_blocked_by_lessons

            blocked, reason = entry_blocked_by_lessons(
                settings,
                symbol,
                side.value if hasattr(side, "value") else str(side),
                run_label=str(getattr(cf, "run_label", "") or ""),
                is_choppy=bool(getattr(cf, "is_choppy", False)),
            )
            if blocked:
                return WinnerVerdict(False, "reject", 0.0, reason)
        except Exception:
            pass

    reasons: list[str] = []
    score = 0.0
    thr = effective_winner_thresholds(settings)
    tuning = get_active_tuning()
    starved = is_entry_starved(settings, tuning)
    from account_guard import universe_fill_active

    universe = universe_fill_active(settings)
    abundant = universe or getattr(settings, "entries_never_pause", False)
    if starved or abundant:
        relax = 0.10 if hourly_3r_active(settings) else (0.08 if universe else 0.06)
        thr = EffectiveWinnerThresholds(
            min_confluence=max(0.50, thr.min_confluence - relax),
            min_agreeing=max(3 if (hourly_3r_active(settings) or universe) else 4, thr.min_agreeing - 1),
            max_opposing=thr.max_opposing + (3 if hourly_3r_active(settings) else 2),
            min_ml_confidence=max(0.52, thr.min_ml_confidence - 0.10),
            min_volume_ratio=max(0.18, thr.min_volume_ratio - 0.55),
            min_score=max(0.50, thr.min_score - 0.06),
            elite_score=max(0.56, thr.elite_score - 0.08),
            apex_score=max(0.62, thr.apex_score - 0.08),
        )

    llm_zone = getattr(decision, "confluence_zone", "") == "llm"
    conf_floor = thr.min_ml_confidence
    if llm_zone and settings.llm_trading_enabled:
        conf_floor = min(conf_floor, settings.llm_trading_min_confidence)

    # --- Hard rejects (BloHunter-style: only trade when the book agrees) ---
    if not cf.htf_aligned:
        return WinnerVerdict(False, "reject", 0.0, "HTF not aligned with 1m direction")

    if len(cf.agreeing) < thr.min_agreeing:
        return WinnerVerdict(
            False, "reject", 0.0, f"agreeing={len(cf.agreeing)} < {thr.min_agreeing}"
        )

    if len(cf.opposing) > thr.max_opposing:
        return WinnerVerdict(
            False, "reject", 0.0, f"opposing={len(cf.opposing)} > {thr.max_opposing}"
        )

    if cf.confluence_score < thr.min_confluence:
        return WinnerVerdict(
            False,
            "reject",
            0.0,
            f"confluence {cf.confluence_score:.2f} < {thr.min_confluence:.2f}",
        )

    if not _side_ok(decision, cf):
        return WinnerVerdict(False, "reject", 0.0, "vote count disagrees with direction")

    exp, exp_n = rolling_expectancy(settings.state_dir, window=24)
    if exp is not None and exp < -0.15 and cf.confluence_score < thr.min_confluence + 0.08:
        return WinnerVerdict(
            False,
            "reject",
            0.0,
            f"live edge negative (E[R]={exp:.2f} n={exp_n}) — need stronger confluence",
        )

    conf = decision.model_confidence or (decision.score / 100.0)
    side = decision.signal
    if (
        settings.ml_block_weak_longs
        and side == Signal.LONG
        and ml_ctx
        and ml_ctx.ready
        and ml_ctx.long_precision < settings.ml_weak_long_precision
    ):
        long_floor = thr.min_ml_confidence + 0.08
        if conf < long_floor:
            return WinnerVerdict(
                False,
                "reject",
                0.0,
                f"long blocked: OOS long_p={ml_ctx.long_precision:.0%} conf {conf:.2f} < {long_floor:.2f}",
            )
        if ml_decision and ml_decision.signal != Signal.LONG:
            return WinnerVerdict(False, "reject", 0.0, "long blocked: ML not aligned on weak long model")

    if conf < conf_floor:
        return WinnerVerdict(
            False, "reject", 0.0, f"conf {conf:.2f} < {conf_floor:.2f}"
        )

    if settings.require_ml_model and settings.signal_mode == "ml":
        if not ml_ready:
            return WinnerVerdict(False, "reject", 0.0, "ML model not ready")
        if ml_decision is None or ml_decision.signal != side:
            return WinnerVerdict(False, "reject", 0.0, "ML direction disagrees with confluence")
    elif ml_ready and ml_decision is not None and ml_decision.signal != side:
        ml_c = ml_decision.model_confidence or (ml_decision.score / 100.0)
        if ml_c >= settings.winner_ml_veto_min_confidence + 0.06:
            return WinnerVerdict(
                False, "reject", 0.0, f"ML strongly disagrees ({ml_decision.signal.value} conf={ml_c:.2f})",
            )

    vol = cf.volume_ratio
    vol_floor = thr.min_volume_ratio
    if llm_zone and starved:
        vol_floor = min(vol_floor, 0.28)
    if vol < vol_floor:
        return WinnerVerdict(
            False, "reject", 0.0, f"volume {vol:.2f} < {vol_floor:.2f}"
        )

    funding = decision.funding_rate
    if side == Signal.LONG and funding is not None and funding > settings.max_funding_long:
        return WinnerVerdict(False, "reject", 0.0, f"funding {funding:.4f} headwind for long")
    if side == Signal.SHORT and funding is not None and funding < settings.min_funding_short:
        return WinnerVerdict(False, "reject", 0.0, f"funding {funding:.4f} headwind for short")

    chase = abs(cf.vwap_distance_pct)
    if chase > settings.winner_max_vwap_chase_pct:
        return WinnerVerdict(False, "reject", 0.0, f"chasing VWAP {chase:.2%}")

    if getattr(settings, "runner_filter_enabled", True):
        is_choppy = getattr(cf, "is_choppy", False) or (
            cf.chop_index >= getattr(settings, "runner_max_chop", 0.56)
            and cf.path_efficiency < getattr(settings, "runner_min_path_eff", 0.26) + 0.04
        )
        min_run = getattr(settings, "runner_min_score", 0.48)
        is_runner = getattr(cf, "is_runner", False) or getattr(cf, "run_score", 0.5) >= min_run
        if is_choppy and not is_runner:
            return WinnerVerdict(
                False,
                "reject",
                0.0,
                f"choppy up/down — chop={cf.chop_index:.0%} path={cf.path_efficiency:.0%}",
            )
        min_run = getattr(settings, "runner_min_score", 0.48)
        if cf.run_score < min_run - 0.12 and cf.regime == "ranging":
            return WinnerVerdict(
                False,
                "reject",
                0.0,
                f"not a steady runner — run={cf.run_score:.2f} path={cf.path_efficiency:.0%}",
            )

    if cf.regime == "ranging" and cf.confluence_score < thr.min_confluence + 0.06:
        return WinnerVerdict(False, "reject", 0.0, "ranging chop — need stronger confluence")

    rr = decision.take_pct / max(decision.stop_pct, 1e-9)
    if settings.scalp_3r_mode and rr < settings.scalp_3r_min_rr * 0.98:
        return WinnerVerdict(False, "reject", 0.0, f"R:R {rr:.2f} below 3R floor")

    # --- Score (rank elite vs good among survivors) ---
    score += cf.confluence_score * 0.35
    score += min(1.0, len(cf.agreeing) / 10.0) * 0.20
    score += conf * 0.25
    score += min(1.0, vol / 2.0) * 0.10
    if cf.htf_aligned:
        score += 0.05
    if ml_decision and ml_decision.signal == side:
        ml_c = ml_decision.model_confidence or (ml_decision.score / 100.0)
        score += ml_c * 0.05
    elif ml_decision and ml_decision.signal != side:
        ml_c = ml_decision.model_confidence or (ml_decision.score / 100.0)
        if ml_c < settings.winner_ml_veto_min_confidence + 0.06:
            score += 0.02
    if cf.regime == "trending":
        score += 0.05
    if getattr(cf, "is_runner", False):
        score += 0.06
    elif getattr(cf, "run_score", 0.5) >= 0.58:
        score += 0.03
    if getattr(cf, "is_choppy", False):
        score -= 0.08
    pick_s = getattr(decision, "pick_score", 0.0) or 0.0
    if pick_s >= settings.pick_min_score + 0.06:
        score += 0.04
    if rr >= settings.scalp_3r_min_rr:
        score += 0.03
    if decision.stop_pct <= settings.scalp_max_stop_pct * 1.05:
        score += 0.02
    score = min(1.0, score)

    if score >= thr.apex_score:
        tier = "apex"
    elif score >= thr.elite_score:
        tier = "elite"
    else:
        tier = "good"
    if settings.winner_elite_only and tier not in ("apex", "elite"):
        return WinnerVerdict(
            False, "reject", score, f"elite-only: score {score:.2f} < {thr.elite_score:.2f}"
        )
    if score < thr.min_score:
        return WinnerVerdict(
            False, "reject", score, f"winner score {score:.2f} < {thr.min_score:.2f}"
        )

    if tier == "apex":
        reasons.append("apex stack")
    elif tier == "elite":
        reasons.append("elite stack")
    log.info(
        "WINNER %s %s tier=%s score=%.2f cf=%.0f%% agree=%d oppose=%d vol=%.2f rr=%.1f:1",
        side.value,
        "pass",
        tier,
        score,
        cf.confluence_score * 100,
        len(cf.agreeing),
        len(cf.opposing),
        vol,
        rr,
    )
    return WinnerVerdict(True, tier, score, "; ".join(reasons) or tier)
