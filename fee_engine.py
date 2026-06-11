"""
FEE OVERCOMING ENGINE - Ensures every winning trade beats fees.
Default protocol: TP must exceed (stop_distance + roundtrip_fees) * 2.5x minimum.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Tuple


# Default fee structure for BloFin futures
# Maker (limit orders): 0.02%
# Taker (market orders): 0.06%
FEE_MAKER = 0.0002
FEE_TAKER = 0.0006


@dataclass
class FeeAnalysis:
    """Complete fee breakdown for a trade decision."""
    entry_fee_pct: float          # fee % on entry
    exit_fee_pct: float           # fee % on exit
    roundtrip_fee_pct: float      # total fee % for full round-trip
    entry_fee_usd: float          # fee $ on entry
    exit_fee_usd: float           # fee $ on exit
    total_fee_usd: float          # total fee $ for round-trip
    min_profit_to_beat_fees_pct: float  # minimum TP % to overcome fees
    min_profit_to_beat_fees_usd: float  # minimum profit $ to overcome fees
    fee_covered: bool             # does the TP plan beat fees?
    profit_after_fees_pct: float  # estimated profit % after fees
    profit_after_fees_usd: float  # estimated profit $ after fees
    safety_margin_pct: float      # how much buffer over fees


def analyze_trade_fees(
    entry_price: float,
    contracts: float,
    contract_size: float,
    stop_distance_pct: float,
    take_distance_pct: float,
    leverage: int,
    taker_fee: float = FEE_TAKER,
    maker_fee: float = FEE_MAKER,
    use_maker_entry: bool = False,
    use_maker_exit: bool = False,
) -> FeeAnalysis:
    """
    Analyze fees for a potential trade and determine if profit beats fees.
    
    Returns FeeAnalysis with fee_covered flag and safety margin.
    """
    notional = entry_price * contracts * contract_size
    
    # Determine if entry/exit is maker or taker
    entry_fee_rate = maker_fee if use_maker_entry else taker_fee
    exit_fee_rate = maker_fee if use_maker_exit else taker_fee
    
    entry_fee_pct = entry_fee_rate
    exit_fee_pct = exit_fee_rate
    roundtrip_fee_pct = entry_fee_pct + exit_fee_pct
    
    entry_fee_usd = notional * entry_fee_pct
    exit_fee_usd = notional * exit_fee_pct
    total_fee_usd = entry_fee_usd + exit_fee_usd
    
    # Minimum *price move* % needed to break even on fees.
    # Fees and PnL both scale linearly with notional, so leverage cancels:
    # required price move = roundtrip_fee_rate (independent of leverage).
    min_profit_to_beat_fees_pct = roundtrip_fee_pct
    
    # Estimated gross profit from take profit move
    gross_profit_pct = take_distance_pct  # as decimal
    gross_profit_usd = notional * gross_profit_pct
    
    profit_after_fees_usd = gross_profit_usd - total_fee_usd
    profit_after_fees_pct = profit_after_fees_usd / (notional / leverage) if notional > 0 else 0
    
    # Fee covered if profit after fees > 0
    fee_covered = profit_after_fees_usd > 0
    
    # Safety margin: how many times fees are covered by profit
    if total_fee_usd > 0:
        safety_margin_pct = ((gross_profit_usd - total_fee_usd) / total_fee_usd) * 100
    else:
        safety_margin_pct = float('inf')
    
    return FeeAnalysis(
        entry_fee_pct=entry_fee_pct * 100,
        exit_fee_pct=exit_fee_pct * 100,
        roundtrip_fee_pct=roundtrip_fee_pct * 100,
        entry_fee_usd=entry_fee_usd,
        exit_fee_usd=exit_fee_usd,
        total_fee_usd=total_fee_usd,
        min_profit_to_beat_fees_pct=min_profit_to_beat_fees_pct * 100,
        min_profit_to_beat_fees_usd=total_fee_usd,
        fee_covered=fee_covered,
        profit_after_fees_pct=profit_after_fees_pct * 100,
        profit_after_fees_usd=profit_after_fees_usd,
        safety_margin_pct=safety_margin_pct,
    )


def ensure_fee_overcoming(
    entry_price: float,
    contracts: float,
    contract_size: float,
    stop_pct: float,
    take_pct: float,
    leverage: int,
    min_fee_coverage_multiple: float = 2.5,  # TP must be at least 2.5x fees
    taker_fee: float = FEE_TAKER,
    maker_fee: float = FEE_MAKER,
    min_rr: float = 1.25,
) -> Tuple[float, float, dict]:
    """
    Adjust take_pct and stop_pct to ensure every winner overcomes fees.
    
    Returns (adjusted_stop_pct, adjusted_take_pct, fee_analysis_dict)
    """
    fee_analysis = analyze_trade_fees(
        entry_price=entry_price,
        contracts=contracts,
        contract_size=contract_size,
        stop_distance_pct=stop_pct,
        take_distance_pct=take_pct,
        leverage=leverage,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
    )
    
    # Fee % on notional
    roundtrip_fee_rate = taker_fee + maker_fee
    
    # The TP must cover: stop_loss_impact + fees * min_fee_coverage_multiple
    # Fee impact on the move: fees eat into the gross profit
    # Required TP = stop_distance + (roundtrip_fees * leverage * min_fee_coverage_multiple)
    
    from liquidation_guard import clamp_stop_take_pct, max_safe_stop_pct

    safe_cap = max_safe_stop_pct(leverage)
    strict_rr = min_rr >= 2.5
    adjusted_stop = min(stop_pct, safe_cap)
    if not strict_rr:
        # Minimum stop = ~10x roundtrip fees so normal fee impact can't trigger SL.
        # Leverage-independent (fees and PnL both scale with notional).
        fee_noise_stop = roundtrip_fee_rate * 10
        adjusted_stop = max(adjusted_stop, min(fee_noise_stop, safe_cap * 0.5))

    required_tp_pct = adjusted_stop + (roundtrip_fee_rate * min_fee_coverage_multiple)
    if strict_rr:
        adjusted_take = adjusted_stop * min_rr
        adjusted_take = max(adjusted_take, required_tp_pct)
    else:
        adjusted_take = max(take_pct, required_tp_pct)

    adjusted_stop, adjusted_take = clamp_stop_take_pct(
        adjusted_stop, adjusted_take, leverage, min_rr=min_rr
    )
    if strict_rr:
        adjusted_take = adjusted_stop * min_rr
    
    # Re-analyze with adjusted values
    adjusted_fee_analysis = analyze_trade_fees(
        entry_price=entry_price,
        contracts=contracts,
        contract_size=contract_size,
        stop_distance_pct=adjusted_stop,
        take_distance_pct=adjusted_take,
        leverage=leverage,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
    )
    
    if not adjusted_fee_analysis.fee_covered:
        # Force TP wider until fees are beaten
        safety_factor = min_fee_coverage_multiple
        while not adjusted_fee_analysis.fee_covered and safety_factor < 10:
            safety_factor += 0.5
            if strict_rr:
                required_tp_pct = adjusted_stop * min_rr
                required_tp_pct = max(
                    required_tp_pct,
                    adjusted_stop + (roundtrip_fee_rate * safety_factor),
                )
                adjusted_take = required_tp_pct
            else:
                required_tp_pct = adjusted_stop + (roundtrip_fee_rate * safety_factor)
                adjusted_take = max(adjusted_take, required_tp_pct)
            adjusted_fee_analysis = analyze_trade_fees(
                entry_price=entry_price,
                contracts=contracts,
                contract_size=contract_size,
                stop_distance_pct=adjusted_stop,
                take_distance_pct=adjusted_take,
                leverage=leverage,
                taker_fee=taker_fee,
                maker_fee=maker_fee,
            )
    
    fee_dict = {
        "roundtrip_fee_pct": round(adjusted_fee_analysis.roundtrip_fee_pct, 4),
        "total_fee_usd": round(adjusted_fee_analysis.total_fee_usd, 4),
        "profit_after_fees_usd": round(adjusted_fee_analysis.profit_after_fees_usd, 4),
        "profit_after_fees_pct": round(adjusted_fee_analysis.profit_after_fees_pct, 4),
        "fee_covered": adjusted_fee_analysis.fee_covered,
        "safety_margin_pct": round(adjusted_fee_analysis.safety_margin_pct, 2),
        "min_profit_to_beat_fees_pct": round(adjusted_fee_analysis.min_profit_to_beat_fees_pct, 4),
    }
    
    return adjusted_stop, adjusted_take, fee_dict


def compute_breakeven_winrate(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    fee_pct: float = FEE_TAKER + FEE_MAKER,
) -> dict:
    """
    Calculate the minimum win rate needed to be profitable after fees.
    Returns dict with breakeven analysis.
    """
    # After-fee win/loss
    after_fee_win = avg_win_pct - fee_pct
    after_fee_loss = avg_loss_pct + fee_pct
    
    if after_fee_win <= 0:
        return {
            "profitable": False,
            "reason": "average win too small to overcome fees",
            "min_win_rate_needed": 1.0,
            "after_fee_edge": 0.0,
        }
    
    # Expected value per trade
    ev = (win_rate * after_fee_win) - ((1 - win_rate) * after_fee_loss)
    
    # Breakeven win rate
    if after_fee_win + after_fee_loss > 0:
        min_wr = after_fee_loss / (after_fee_win + after_fee_loss)
    else:
        min_wr = 1.0
    
    return {
        "profitable": ev > 0,
        "ev_per_trade_pct": round(ev * 100, 4),
        "min_win_rate_needed": round(min_wr * 100, 2),
        "after_fee_avg_win_pct": round(after_fee_win * 100, 4),
        "after_fee_avg_loss_pct": round(after_fee_loss * 100, 4),
        "after_fee_edge": round((win_rate * after_fee_win) - ((1 - win_rate) * after_fee_loss), 6),
    }


# Pre-computed fee table for quick lookup
FEE_TABLE = {
    "taker_entry_taker_exit": FEE_TAKER + FEE_TAKER,     # 0.12%
    "maker_entry_taker_exit": FEE_MAKER + FEE_TAKER,     # 0.08%
    "taker_entry_maker_exit": FEE_TAKER + FEE_MAKER,     # 0.08%
    "maker_entry_maker_exit": FEE_MAKER + FEE_MAKER,     # 0.04%
}


def roundtrip_fee_pct(use_maker_entry: bool = False, use_maker_exit: bool = False) -> float:
    """Get the roundtrip fee percentage for a given execution style."""
    if use_maker_entry and use_maker_exit:
        return FEE_MAKER + FEE_MAKER
    elif use_maker_entry:
        return FEE_MAKER + FEE_TAKER
    elif use_maker_exit:
        return FEE_TAKER + FEE_MAKER
    else:
        return FEE_TAKER + FEE_TAKER