"""
Scalp profiles: momentum (default) or strict 3R (SCALP_3R_MODE=true).

3R mode: hard 1R stop, 3R take (recomputed after exchange liq SL), accepts losses.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Settings


@dataclass(frozen=True)
class ScalpProfile:
    base_leverage: int = 20
    max_leverage_cap: int = 50
    poll_seconds_base: int = 12
    min_entry_gap_seconds: float = 35.0
    min_take_profit_pct: float = 0.006
    symbol_cooldown_minutes: int = 5
    atr_stop_mult: float = 1.15
    atr_take_mult: float = 2.1
    max_stop_pct: float = 0.022
    max_take_pct: float = 0.045
    min_hold_seconds: float = 40.0
    harvest_fee_mult: float = 1.75
    steward_min_interval: float = 4.0
    fee_coverage_multiple: float = 2.0
    ml_forward_bars: int = 2
    ml_label_threshold: float = 0.0010
    margin_deploy_base: float = 0.22
    margin_deploy_max: float = 0.50
    margin_use_fraction: float = 0.88
    three_r_mode: bool = False
    min_rr: float = 1.35
    harvest_min_r: float = 0.0  # 0 = legacy % of TP progress; >0 = min R multiple to harvest
    min_signal_score_bump: float = 0.0
    min_confidence_bump: float = 0.0
    enforce_tp_from_sl: bool = False

    @classmethod
    def from_settings(cls, s: Settings) -> ScalpProfile:
        three_r = s.scalp_3r_mode
        min_rr = s.scalp_3r_min_rr if three_r else 1.35
        max_stop = s.scalp_max_stop_pct
        max_take = s.scalp_max_take_pct
        try:
            from runner_momentum import runner_priority_active

            runner_priority = runner_priority_active(s)
        except Exception:
            runner_priority = False
        # Throughput brackets apply unless this entry is a momentum runner (signals.py tags those).
        fast_3r = three_r and (
            getattr(s, "scalp_fast_3r", False) or getattr(s, "hourly_3r_winner_mode", False)
        )
        if fast_3r:
            # Tight brackets for high win-rate / hour (exchange TP/SL, not wide liq-gap stops).
            max_stop = min(max_stop, float(getattr(s, "scalp_fast_max_stop_pct", 0.008) or 0.008))
            max_take = min(
                max_take,
                float(getattr(s, "scalp_fast_max_take_pct", 0.0) or 0)
                or max_stop * min_rr * 1.02,
            )
            min_hold_seconds = (
                max(s.scalp_min_hold_seconds, 55.0)
                if getattr(s, "stack_winners_mode", True)
                else min(s.scalp_min_hold_seconds, 18.0)
            )
        elif three_r:
            max_take = max(max_take, max_stop * min_rr * 1.05)
        return cls(
            base_leverage=s.scalp_leverage,
            max_leverage_cap=s.scalp_leverage_max,
            poll_seconds_base=s.scalp_poll_seconds,
            min_entry_gap_seconds=s.scalp_entry_gap_seconds,
            min_take_profit_pct=s.scalp_min_take_profit_pct,
            symbol_cooldown_minutes=s.scalp_cooldown_minutes,
            atr_stop_mult=s.scalp_atr_stop_mult,
            atr_take_mult=s.scalp_atr_take_mult,
            max_stop_pct=max_stop,
            max_take_pct=max_take,
            min_hold_seconds=s.scalp_min_hold_seconds,
            harvest_fee_mult=s.scalp_harvest_fee_mult,
            steward_min_interval=s.scalp_steward_interval,
            fee_coverage_multiple=s.scalp_fee_coverage_mult,
            ml_forward_bars=s.ml_forward_bars,
            ml_label_threshold=s.ml_label_threshold,
            margin_use_fraction=s.margin_use_fraction,
            three_r_mode=three_r,
            min_rr=min_rr,
            harvest_min_r=(
                min(s.scalp_3r_harvest_min_r, 1.0)
                if fast_3r and not getattr(s, "stack_winners_mode", True)
                else (
                    min(s.scalp_3r_harvest_min_r, 1.35)
                    if fast_3r
                    else (
                        max(s.scalp_3r_harvest_min_r, 2.5)
                        if three_r and getattr(s, "hourly_3r_winner_mode", False)
                        else (s.scalp_3r_harvest_min_r if three_r else 0.0)
                    )
                )
            ),
            min_signal_score_bump=s.scalp_3r_min_score_bump if three_r else 0.0,
            min_confidence_bump=s.scalp_3r_min_confidence_bump if three_r else 0.0,
            enforce_tp_from_sl=three_r,
        )


def profile_for(settings: Settings) -> ScalpProfile | None:
    if not settings.scalp_mode:
        return None
    return ScalpProfile.from_settings(settings)
