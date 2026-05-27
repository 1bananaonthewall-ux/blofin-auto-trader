"""Margin-aware position sizing with anti-liquidation and fee-overcoming."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from fee_engine import ensure_fee_overcoming
from liquidation_guard import (
    clamp_stop_take_pct,
    enforce_risk_reward,
    liquidation_distance_pct,
    margin_rate,
    max_safe_stop_pct,
    size_for_min_margin_rate,
)

log = logging.getLogger(__name__)


def margin_required_usdt(
    entry_price: float,
    contracts: float,
    contract_size: float,
    leverage: int,
) -> float:
    notional = contracts * contract_size * entry_price
    return notional / max(leverage, 1)


@dataclass
class TradePlan:
    contracts: float
    leverage: int
    stop_pct: float
    take_pct: float
    margin_usd: float
    fee_covered: bool
    profit_after_fees_usd: float
    model_confidence: float
    margin_rate: float = 1.0
    notional_usdt: float = 0.0


class MarginAwareSizer:
    """Size trades so margin rate is adequate; SL uses exchange liq after fill."""

    def __init__(
        self,
        *,
        free_margin: float,
        fee_taker: float,
        fee_maker: float,
        min_take_profit_pct: float,
        base_leverage: int,
        max_leverage: int,
        margin_reserve_usdt: float,
        risk_fraction: float,
        model_confidence: float,
        liquidation_buffer: float = 1.35,
        scalp_mode: bool = False,
        max_stop_pct: float = 0.08,
        max_take_pct: float = 0.15,
        fee_coverage_multiple: float = 2.0,
        margin_use_fraction: float = 0.88,
        min_margin_rate: float = 0.92,
        min_rr: float = 1.35,
    ) -> None:
        self.free_margin = max(0.0, free_margin)
        self.fee_taker = fee_taker
        self.fee_maker = fee_maker
        self.min_take_profit_pct = min_take_profit_pct
        self.base_leverage = base_leverage
        self.max_leverage = max_leverage
        self.margin_reserve = margin_reserve_usdt
        self.risk_fraction = risk_fraction
        self.model_confidence = max(0.0, min(1.0, model_confidence))
        self.liquidation_buffer = liquidation_buffer
        self.scalp_mode = scalp_mode
        self.max_stop_pct = max_stop_pct
        self.max_take_pct = max_take_pct
        self.fee_coverage_multiple = fee_coverage_multiple
        self.margin_use_fraction = margin_use_fraction
        self.min_margin_rate = max(0.5, min(1.0, min_margin_rate))
        self.min_rr = max(1.0, min_rr)

    def _leverage_for_confidence(self) -> int:
        conf = self.model_confidence
        if self.scalp_mode:
            if self.min_rr >= 2.5:
                return self.max_leverage
            if conf >= 0.82:
                lev = self.max_leverage
            elif conf >= 0.76:
                lev = int(self.base_leverage + (self.max_leverage - self.base_leverage) * 0.85)
            elif conf >= 0.70:
                lev = int(self.base_leverage + (self.max_leverage - self.base_leverage) * 0.55)
            else:
                lev = self.base_leverage
            lev = max(self.base_leverage, min(self.max_leverage, lev))
        else:
            if conf >= 0.80:
                lev = min(self.max_leverage, self.base_leverage * 2)
            elif conf >= 0.72:
                lev = int(self.base_leverage * 1.5)
            elif conf >= 0.65:
                lev = self.base_leverage
            else:
                lev = max(3, self.base_leverage // 2)
            lev = max(3, min(self.max_leverage, lev))

        return lev

    def plan_trade(
        self,
        entry_price: float,
        stop_pct: float,
        take_pct: float,
        contract_size: float,
        min_size: float,
        *,
        margin_fraction: float | None = None,
    ) -> TradePlan | None:
        if entry_price <= 0 or contract_size <= 0 or min_size <= 0:
            return None

        usable = self.free_margin - self.margin_reserve
        if usable < 0.01:
            return None

        lev = self._leverage_for_confidence()
        safe_cap = max_safe_stop_pct(lev)
        scalp_cap = min(self.max_stop_pct, safe_cap) if self.scalp_mode else safe_cap

        stop_pct, take_pct = clamp_stop_take_pct(
            min(stop_pct, scalp_cap),
            take_pct,
            lev,
            min_rr=self.min_rr if self.scalp_mode else 1.5,
        )

        rt_fee = (self.fee_taker + self.fee_maker) * 100
        fee_mult = 2.0 if self.scalp_mode else 2.5
        min_tp = max(self.min_take_profit_pct * 100, rt_fee * fee_mult) / 100.0
        strict_3r = self.scalp_mode and self.min_rr >= 2.5
        if strict_3r:
            rr_fit = enforce_risk_reward(
                stop_pct,
                take_pct,
                min_rr=self.min_rr,
                strict=True,
                max_stop_pct=self.max_stop_pct,
                max_take_pct=self.max_take_pct,
            )
            if rr_fit is None:
                log.info(
                    "skip: 3R caps prevent %.1f:1 (stop=%.2f%% max_take=%.2f%%)",
                    self.min_rr,
                    stop_pct * 100,
                    self.max_take_pct * 100,
                )
                return None
            stop_pct, take_pct = rr_fit
        else:
            if take_pct < min_tp:
                take_pct = min_tp
            if take_pct < stop_pct * self.min_rr:
                take_pct = stop_pct * self.min_rr
            if self.scalp_mode:
                take_pct = min(self.max_take_pct, take_pct)
            stop_pct, take_pct = clamp_stop_take_pct(stop_pct, take_pct, lev, min_rr=self.min_rr)
            if take_pct < stop_pct * self.min_rr * 0.98:
                log.info(
                    "skip: TP cap prevents %.1fR (stop=%.2f%% take=%.2f%%)",
                    self.min_rr,
                    stop_pct * 100,
                    take_pct * 100,
                )
                return None

        if margin_fraction is not None and margin_fraction > 0:
            margin_budget = usable * min(margin_fraction, 0.92)
        else:
            margin_budget = usable * max(self.risk_fraction * 2.5, 0.18)

        target_margin = margin_budget * self.margin_use_fraction

        sized = size_for_min_margin_rate(
            entry_price=entry_price,
            contract_size=contract_size,
            min_size=min_size,
            target_margin_usdt=target_margin,
            leverage=lev,
            min_margin_rate=self.min_margin_rate,
            margin_budget_usdt=margin_budget,
            fixed_leverage=strict_3r,
        )
        if sized is None and strict_3r:
            log.info(
                "skip: cannot fit min lot at %dx within margin $%.3f (use cheaper symbol)",
                lev,
                margin_budget,
            )
            return None
        if sized:
            contracts, lev = sized
        else:
            raw = (target_margin * lev) / (entry_price * contract_size)
            contracts = max(min_size, math.floor(raw / min_size) * min_size)

        margin = margin_required_usdt(entry_price, contracts, contract_size, lev)
        notional = contracts * contract_size * entry_price
        mrate = margin_rate(notional, margin, lev)

        while margin > margin_budget and contracts >= min_size * 2:
            contracts -= min_size
            margin = margin_required_usdt(entry_price, contracts, contract_size, lev)
            notional = contracts * contract_size * entry_price
            mrate = margin_rate(notional, margin, lev)

        if margin > usable or contracts < min_size:
            return None

        min_mrate = self.min_margin_rate * (0.72 if strict_3r else 0.85)
        if mrate < min_mrate:
            log.warning(
                "skip %dx: margin rate %.0f%% < min %.0f%% (margin=$%.3f notional=$%.3f)",
                lev,
                mrate * 100,
                self.min_margin_rate * 100,
                margin,
                notional,
            )
            return None

        adjusted_stop, adjusted_take, fee_info = ensure_fee_overcoming(
            entry_price=entry_price,
            contracts=contracts,
            contract_size=contract_size,
            stop_pct=stop_pct,
            take_pct=take_pct,
            leverage=lev,
            min_fee_coverage_multiple=self.fee_coverage_multiple,
            taker_fee=self.fee_taker,
            maker_fee=self.fee_maker,
            min_rr=self.min_rr,
        )
        if strict_3r:
            rr_fit = enforce_risk_reward(
                adjusted_stop,
                adjusted_take,
                min_rr=self.min_rr,
                strict=True,
                max_stop_pct=self.max_stop_pct,
                max_take_pct=self.max_take_pct,
            )
            if rr_fit is None:
                log.info("skip: fee-adjusted 3R does not fit caps")
                return None
            adjusted_stop, adjusted_take = rr_fit
        else:
            adjusted_stop, adjusted_take = clamp_stop_take_pct(
                adjusted_stop, adjusted_take, lev, min_rr=self.min_rr
            )
        if not fee_info["fee_covered"]:
            return None

        final_margin = margin_required_usdt(entry_price, contracts, contract_size, lev)
        log.info(
            "sizing %dx lev margin=$%.3f (%.0f%% budget) notional=$%.3f margin_rate=%.0f%% "
            "stop=%.2f%% take=%.2f%% rr=%.2f:1 liq_dist=%.2f%%",
            lev,
            final_margin,
            100 * final_margin / max(margin_budget, 0.01),
            notional,
            mrate * 100,
            adjusted_stop * 100,
            adjusted_take * 100,
            adjusted_take / max(adjusted_stop, 1e-9),
            liquidation_distance_pct(lev) * 100,
        )

        return TradePlan(
            contracts=contracts,
            leverage=lev,
            stop_pct=adjusted_stop,
            take_pct=adjusted_take,
            margin_usd=final_margin,
            fee_covered=True,
            profit_after_fees_usd=fee_info["profit_after_fees_usd"],
            model_confidence=self.model_confidence,
            margin_rate=mrate,
            notional_usdt=notional,
        )
