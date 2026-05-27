from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from blofin_http import BlofinHttp
from treasury.blofin_side import (
    futures_equity_usd,
    min_deposit_for_chain,
    process_new_deposits,
    transfer_deposit_to_futures,
)
from treasury.scanners import Holding, scan_wallet
from treasury.settings import TreasurySettings, load_treasury_settings
from treasury.state_store import TreasuryState, load_state, save_state
from treasury.sweeper import build_sweep_plan, execute_sweep
from treasury.wallets import WalletRegistry, load_wallet_registry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanSummary:
    holdings: list[Holding]
    total_usd: float
    usdt_usd: float


def scan_all_wallets(registry: WalletRegistry) -> ScanSummary:
    holdings: list[Holding] = []
    for wallet in registry.wallets:
        try:
            holdings.extend(scan_wallet(wallet))
        except Exception as e:
            log.warning("scan failed %s: %s", wallet.id, e)
    total = sum(h.usd_value for h in holdings)
    usdt = sum(h.usd_value for h in holdings if h.symbol in {"USDT", "USDC"})
    return ScanSummary(holdings=holdings, total_usd=total, usdt_usd=usdt)


def run_cycle(settings: TreasurySettings | None = None) -> dict[str, float | int | bool]:
    settings = settings or load_treasury_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)

    registry = load_wallet_registry(settings.wallets_path)
    state_path = settings.state_dir / "treasury.json"
    state = load_state(state_path)

    http = BlofinHttp(
        settings.api_key,
        settings.secret,
        settings.passphrase,
        demo=False,
    )

    summary = scan_all_wallets(registry)
    state.last_scan_ts = time.time()
    state.last_total_usd = summary.total_usd

    sweeps_this_cycle = 0
    usdt_remaining = summary.usdt_usd
    while usdt_remaining >= settings.sweep_target_usd:
        plan = build_sweep_plan(summary.holdings, settings)
        if not plan:
            break
        ok = execute_sweep(plan, settings)
        if not ok:
            break
        sweeps_this_cycle += 1
        state.sweeps_completed += 1
        state.total_swept_usd += plan.amount_usd
        state.last_sweep_ts = time.time()
        usdt_remaining -= plan.amount_usd

    seen, credited = process_new_deposits(http, settings, state.seen_deposit_ids or [])
    state.seen_deposit_ids = seen
    if credited > 0 and settings.auto_futures_transfer:
        transfer_deposit_to_futures(
            http, settings, credited, dry_run=settings.dry_run
        )

    min_dep = min_deposit_for_chain(
        http, settings.deposit_chain, settings.target_currency
    )
    equity = futures_equity_usd(http)

    save_state(state_path, state)

    log.info(
        "treasury cycle: wallets=%d total=$%.2f stables=$%.2f sweeps=%d "
        "blofin_futures=$%.2f min_deposit=%.4f dry_run=%s",
        len(registry.wallets),
        summary.total_usd,
        summary.usdt_usd,
        sweeps_this_cycle,
        equity,
        min_dep,
        settings.dry_run,
    )

    return {
        "total_usd": summary.total_usd,
        "usdt_usd": summary.usdt_usd,
        "sweeps": sweeps_this_cycle,
        "blofin_equity": equity,
        "sweeps_completed": state.sweeps_completed,
        "dry_run": settings.dry_run,
    }
