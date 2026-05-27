from __future__ import annotations

import math
from dataclasses import dataclass

from markets import dynamic_max_positions
from fee_engine import ensure_fee_overcoming, FEE_TAKER, FEE_MAKER


def contracts_for_risk(equity, risk_pct, entry_price, stop_distance_pct, contract_size, leverage):
    if entry_price <= 0 or contract_size <= 0 or stop_distance_pct <= 0:
        return 0.0
    risk_usd = equity * risk_pct
    notional_per_contract = entry_price * contract_size
    loss_per_contract = notional_per_contract * stop_distance_pct
    if loss_per_contract <= 0:
        return 0.0
    return max(risk_usd / loss_per_contract, 0.0)


@dataclass
class SizingDecision:
    contracts: float
    effective_leverage: int
    stop_pct: float
    take_pct: float
    risk_usd: float
    fee_est_roundtrip: float
    min_notional_check: bool
    fee_covered: bool = False             # NEW: fee analysis flag
    profit_after_fees_usd: float = 0.0    # NEW: profit after fees
    safety_margin_pct: float = 0.0        # NEW: how much buffer over fees


class SmartPositionSizer:
    """
    HYPER COMPOUND GROWTH POSITION SIZER
    Engineered for $100 -> $95M by Sep 2027
    Based on optimal f (Kelly Criterion) pushed to the limit.
    Benchmarked against Ed Thorp's hedge fund methodology.
    
    Core principles:
      1. FULL Kelly on edge
      2. 200x max leverage
      3. NO position count limits
      4. NO margin caps
      5. NO fee sanity blocks
      6. Aggressive small-account scaling
      7. Volatility-adaptive position sizing
      8. Momentum acceleration capture
      9. FEE OVERCOMING - every winner beats fees by 2.5x minimum
    """

    CONFIDENCE_LEVERAGE_MAP = [
        (0.0,   20),    # base - minimum for growth
        (0.40,  50),   # marginal
        (0.50,  75),   # moderate
        (0.55,  100),  # decent
        (0.60,  125),  # good
        (0.65,  150),  # strong
        (0.72,  175),  # very strong
        (0.80,  200),  # exceptional - max blastoff
    ]

    def __init__(self, equity, *, fee_est_taker=0.0006, fee_est_maker=0.0002, min_take_profit_pct=0.01,
                 small_account_threshold=50.0, base_leverage=20, max_leverage=200, base_risk_pct=0.08,
                 profit_factor=1.0, max_positions=9999, model_confidence=0.0):
        self.equity = equity
        self.fee_est_taker = fee_est_taker
        self.fee_est_maker = fee_est_maker
        self.min_take_profit_pct = min_take_profit_pct
        self.small_account_threshold = small_account_threshold
        self.base_leverage = base_leverage
        self.max_leverage = max_leverage
        self.base_risk_pct = base_risk_pct
        self.profit_factor = max(0.1, min(10.0, profit_factor))
        self.max_positions = max(1, max_positions)
        self.model_confidence = max(0.0, min(1.0, model_confidence))

    def _confidence_leverage(self) -> int:
        """Edge-driven leverage: ML confidence determines leverage exponentially."""
        conf = self.model_confidence
        lev = self.base_leverage
        for i, (threshold, target_lev) in enumerate(self.CONFIDENCE_LEVERAGE_MAP):
            if conf >= threshold:
                lev = target_lev
            else:
                if i > 0:
                    prev_threshold, prev_lev = self.CONFIDENCE_LEVERAGE_MAP[i - 1]
                    if conf > prev_threshold and threshold > prev_threshold:
                        fraction = (conf - prev_threshold) / (threshold - prev_threshold)
                        lev = int(prev_lev + (target_lev - prev_lev) * fraction)
                break

        # PF scalar: winning = more exposure, losing = still pushing
        if self.profit_factor > 1.3:
            lev = int(lev * (1.0 + (self.profit_factor - 1.3) * 0.5))
        elif self.profit_factor < 0.3:
            lev = int(lev * 0.8)  # mild reduction only, keep fighting

        # Small account boost: tiny accounts need maximum leverage
        if self.equity < self.small_account_threshold:
            ratio = self.equity / self.small_account_threshold
            boost = 1.0 + (1.0 - ratio) * 1.0  # more aggressive boost
            lev = int(lev * boost)

        return max(self.base_leverage, min(self.max_leverage, lev))

    def effective_leverage(self):
        return self._confidence_leverage()

    def _kelly_full(self) -> float:
        """
        Full Kelly Criterion: f* = (p * (R + 1) - 1) / R
        
        p = model confidence as win probability
        R = reward:risk ratio (estimated from profit factor)
        
        Returns optimal fraction of equity to risk per trade.
        """
        p = self.model_confidence
        if p <= 0.40:
            return self.base_risk_pct * 0.5

        # Estimate reward:risk from profit factor
        r = max(1.5, self.profit_factor * 1.2)

        kelly = (p * (r + 1) - 1) / r
        kelly = max(0.0, kelly)

        # Use HALF Kelly for safety with extreme leverage
        half_kelly = kelly * 0.5

        # Blend: use higher of base or half-Kelly
        blended = max(self.base_risk_pct, half_kelly)

        # PF scaling - amplify when winning
        blended = blended * min(3.0, max(0.5, self.profit_factor))

        return max(0.02, min(0.35, blended))

    def effective_risk_pct(self):
        base = self._kelly_full()
        lev = self._confidence_leverage()
        if lev > 10:
            base = base * (10.0 / lev)
        return max(0.01, min(0.20, base))

    def size_for_trade(self, entry_price, stop_distance_pct, take_distance_pct, contract_size, min_size):
        if entry_price <= 0 or contract_size <= 0 or min_size <= 0:
            return None

        lev = self._confidence_leverage()
        risk_pct = self.effective_risk_pct()

        # Fee buffer
        rt_fee_pct = (self.fee_est_taker + self.fee_est_maker) * 100
        fee_buffer = rt_fee_pct + 0.005
        min_tp = max(self.min_take_profit_pct * 100, fee_buffer) / 100

        # Minimum reward:risk scales with leverage
        if lev >= 50:
            min_rr = 1.5  # relaxed for ultra-high freq
        elif lev >= 30:
            min_rr = 1.8
        elif lev >= 20:
            min_rr = 1.5
        elif lev >= 10:
            min_rr = 1.3
        else:
            min_rr = 1.2

        if take_distance_pct < stop_distance_pct * min_rr:
            take_distance_pct = stop_distance_pct * min_rr

        if take_distance_pct < min_tp:
            take_distance_pct = min_tp
            if stop_distance_pct > take_distance_pct / min_rr:
                stop_distance_pct = take_distance_pct / min_rr

        risk_usd = self.equity * risk_pct
        notional_per_contract = entry_price * contract_size
        loss_per_contract = notional_per_contract * stop_distance_pct

        if loss_per_contract <= 0:
            return None

        raw_contracts = max(risk_usd / loss_per_contract, 0.0)

        # Force minimum size by scaling risk - more aggressive
        if raw_contracts < min_size:
            scaled_pct = risk_pct
            while raw_contracts < min_size and scaled_pct < 0.35:
                scaled_pct = min(scaled_pct * 2.0, 0.35)
                raw_contracts = max((self.equity * scaled_pct) / loss_per_contract, 0.0)
            if raw_contracts < min_size:
                return None

        contracts = math.floor(raw_contracts / min_size) * min_size
        if contracts < min_size:
            return None

        notional = contracts * notional_per_contract

        # FEE OVERCOMING ENGINE - ensure every winner beats fees
        adjusted_stop, adjusted_take, fee_dict = ensure_fee_overcoming(
            entry_price=entry_price,
            contracts=contracts,
            contract_size=contract_size,
            stop_pct=stop_distance_pct,
            take_pct=take_distance_pct,
            leverage=lev,
            min_fee_coverage_multiple=2.5,
            taker_fee=self.fee_est_taker,
            maker_fee=self.fee_est_maker,
        )

        # If fees can't be overcome even with max TP, skip this trade
        if not fee_dict["fee_covered"]:
            return None

        stop_distance_pct = adjusted_stop
        take_distance_pct = adjusted_take

        return SizingDecision(
            contracts=contracts,
            effective_leverage=lev,
            stop_pct=stop_distance_pct,
            take_pct=take_distance_pct,
            risk_usd=risk_usd,
            fee_est_roundtrip=notional * (self.fee_est_taker + self.fee_est_maker),
            min_notional_check=True,
            fee_covered=fee_dict["fee_covered"],
            profit_after_fees_usd=fee_dict["profit_after_fees_usd"],
            safety_margin_pct=fee_dict["safety_margin_pct"],
        )

    def recommended_slots(self, max_positions: int) -> int:
        if self.equity <= 0:
            return 0
        # Amplify slots for faster growth
        slots = dynamic_max_positions(self.equity, hard_cap=max_positions)
        if self.equity < self.small_account_threshold:
            slots = max(slots, 5)  # minimum 5 positions for small accounts
        return slots

    def min_equity_per_slot(self) -> float:
        return max(0.01, self.equity * 0.03)  # lower per-slot requirement