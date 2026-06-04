"""Margin-aware position sizing with anti-liquidation and fee-overcoming."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from fee_engine import ensure_fee_overcoming
from liquidation_guard import (
    achievable_margin_rates,
    clamp_stop_take_pct,
    enforce_risk_reward,
    liquidation_distance_pct,
    margin_rate,
    max_notional_for_margin_budget,
    max_safe_stop_pct,
    open_stop_within_liq_room,
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
        min_margin_rate: float = 1.08,
        target_margin_rate: float = 1.15,
        max_effective_leverage: int = 32,
        max_stop_liq_fraction: float = 0.26,
        min_rr: float = 1.35,
        micro_equity_threshold: float = 10.0,
        small_account_threshold: float = 50.0,
        margin_top_up_enabled: bool = False,
        skip_liq_room_check: bool = False,
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
        self.min_margin_rate = max(1.0, min(float(min_margin_rate), 1.35))
        self.target_margin_rate = max(self.min_margin_rate, min(float(target_margin_rate), 1.40))
        # Sizing uses mission leverage; margin_rate (not a lower lev cap) prevents liquidation.
        self.max_effective_leverage = max(3, int(max_effective_leverage))
        self._leverage_cap = max(self.max_leverage, self.max_effective_leverage)
        self.max_stop_liq_fraction = max(0.08, min(0.45, float(max_stop_liq_fraction)))
        self.min_rr = max(1.0, min_rr)
        self.micro_equity_threshold = float(micro_equity_threshold)
        self.small_account_threshold = float(small_account_threshold)
        self.margin_top_up_enabled = bool(margin_top_up_enabled)
        self.skip_liq_room_check = bool(skip_liq_room_check)

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

        return max(3, min(lev, self.max_leverage))

    def plan_trade(
        self,
        entry_price: float,
        stop_pct: float,
        take_pct: float,
        contract_size: float,
        min_size: float,
        *,
        margin_fraction: float | None = None,
        equity: float = 0.0,
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

        class _RateCtx:
            min_margin_rate = self.min_margin_rate
            target_margin_rate = self.target_margin_rate
            micro_equity_threshold = self.micro_equity_threshold
            small_account_threshold = self.small_account_threshold
            margin_top_up_enabled = self.margin_top_up_enabled

        min_mrate, tgt_mrate = achievable_margin_rates(_RateCtx(), equity)

        micro_budget = equity > 0 and equity < self.micro_equity_threshold * 2.5
        if margin_fraction is not None and margin_fraction > 0:
            cap_frac = 0.88 if micro_budget else 0.42
            margin_budget = usable * min(margin_fraction, cap_frac)
        else:
            margin_budget = usable * (0.75 if micro_budget else max(self.risk_fraction * 1.8, 0.12))

        use_frac = 0.92 if micro_budget else min(self.margin_use_fraction, 0.72)
        margin_budget = min(margin_budget, usable * use_frac)
        target_margin = margin_budget * (0.95 if micro_budget else use_frac)

        sized = size_for_min_margin_rate(
            entry_price=entry_price,
            contract_size=contract_size,
            min_size=min_size,
            target_margin_usdt=target_margin,
            leverage=lev,
            min_margin_rate=tgt_mrate,
            margin_budget_usdt=margin_budget,
            fixed_leverage=False,
        )
        if sized is None:
            sized = size_for_min_margin_rate(
                entry_price=entry_price,
                contract_size=contract_size,
                min_size=min_size,
                target_margin_usdt=target_margin,
                leverage=lev,
                min_margin_rate=tgt_mrate,
                margin_budget_usdt=margin_budget,
                fixed_leverage=strict_3r,
            )
        if sized is None:
            log.info(
                "sizing fallback at %dx within margin $%.3f (margin-rate fit failed, trying notional cap)",
                lev,
                margin_budget,
            )
        if sized:
            contracts, lev = sized
        else:
            max_notional = max_notional_for_margin_budget(
                target_margin, lev, tgt_mrate
            )
            raw = max_notional / (entry_price * contract_size)
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

        while mrate < min_mrate and contracts >= min_size * 2:
            contracts -= min_size
            margin = margin_required_usdt(entry_price, contracts, contract_size, lev)
            notional = contracts * contract_size * entry_price
            mrate = margin_rate(notional, margin, lev)
        if mrate < min_mrate:
            min_margin = min_size * contract_size * entry_price / lev
            if min_margin <= margin_budget + 0.001 and min_margin <= usable:
                contracts = min_size
                margin = min_margin
                notional = contracts * contract_size * entry_price
                mrate = margin_rate(notional, margin, lev)
            if mrate < min_mrate:
                log.warning(
                    "skip %dx: margin rate %.0f%% < floor %.0f%% "
                    "(free=$%.2f budget=$%.2f min_lot_margin=$%.3f — need cheaper symbol or more balance)",
                    lev,
                    mrate * 100,
                    min_mrate * 100,
                    usable,
                    margin_budget,
                    min_margin,
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

        max_loss_usd = notional * adjusted_stop
        if equity > 0 and max_loss_usd > equity * 0.04:
            while contracts >= min_size * 2 and max_loss_usd > equity * 0.04:
                contracts -= min_size
                margin = margin_required_usdt(entry_price, contracts, contract_size, lev)
                notional = contracts * contract_size * entry_price
                mrate = margin_rate(notional, margin, lev)
                max_loss_usd = notional * adjusted_stop
            if max_loss_usd > equity * 0.04 or mrate < min_mrate:
                log.info(
                    "skip: SL risk $%.2f > 4%% equity ($%.2f) at %dx",
                    max_loss_usd,
                    equity * 0.04,
                    lev,
                )
                return None

        if not self.skip_liq_room_check:
            liq_frac = self.max_stop_liq_fraction
            if strict_3r:
                liq_dist = liquidation_distance_pct(lev)
                # Fast 3R stop (~1%) must fit inside entry→liq cushion, not only 26% of that span.
                liq_frac = max(liq_frac, min(0.45, (adjusted_stop / max(liq_dist, 1e-9)) * 1.05))
            if not open_stop_within_liq_room(
                adjusted_stop, lev, max_fraction_of_liq_dist=liq_frac
            ):
                log.info(
                    "skip %dx: stop %.2f%% too tight vs liq room %.2f%% (raise margin or lower lev)",
                    lev,
                    adjusted_stop * 100,
                    liquidation_distance_pct(lev) * 100,
                )
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
