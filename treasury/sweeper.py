from __future__ import annotations

import logging
from dataclasses import dataclass

from treasury.scanners import Holding
from treasury.settings import TreasurySettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepPlan:
    amount_usd: float
    currency: str
    chain: str
    deposit_address: str
    sources: list[Holding]


def build_sweep_plan(
    holdings: list[Holding],
    settings: TreasurySettings,
    *,
    target_usd: float | None = None,
) -> SweepPlan | None:
    """Pick USDT/stable holdings until target USD is covered (simple v1)."""
    target = target_usd or settings.sweep_target_usd
    stable = [h for h in holdings if h.symbol in {"USDT", "USDC"} and h.usd_value > 0]
    stable.sort(key=lambda h: h.usd_value, reverse=True)
    picked: list[Holding] = []
    total = 0.0
    for h in stable:
        if total >= target:
            break
        picked.append(h)
        total += h.usd_value
    if total < target:
        return None
    return SweepPlan(
        amount_usd=target,
        currency=settings.target_currency,
        chain=settings.deposit_chain,
        deposit_address=settings.deposit_address,
        sources=picked,
    )


def execute_sweep(plan: SweepPlan, settings: TreasurySettings) -> bool:
    """
    On-chain send to Blofin deposit address.
    v1: logs intent; enable live sends once private keys + chain libs are configured.
    """
    if not settings.deposit_address:
        log.error(
            "Set BLOFIN_USDT_DEPOSIT_ADDRESS in .env (copy from Blofin → Assets → Deposit)"
        )
        return False

    if settings.dry_run:
        log.info(
            "DRY_RUN sweep $%.2f %s via %s -> %s from %d wallet(s)",
            plan.amount_usd,
            plan.currency,
            plan.chain,
            plan.deposit_address[:8] + "...",
            len(plan.sources),
        )
        for h in plan.sources:
            log.info(
                "  would send from %s (%s): %.6f %s (~$%.2f)",
                h.wallet_id,
                h.chain,
                h.amount,
                h.symbol,
                h.usd_value,
            )
        return True

    # Live on-chain transfers require chain-specific signing (web3 / tronpy).
    has_evm = bool(settings.evm_private_key)
    has_tron = bool(settings.tron_private_key)
    if not has_evm and not has_tron:
        log.error(
            "Live sweep needs TREASURY_EVM_PRIVATE_KEY and/or TREASURY_TRON_PRIVATE_KEY "
            "(or implement per-wallet keys). Staying in log-only mode."
        )
        return False

    log.warning(
        "Live on-chain sweep is not fully wired yet; configure keys then extend treasury/sweeper.py"
    )
    return False
