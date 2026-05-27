from __future__ import annotations

import logging
import time
import uuid

from blofin_http import BlofinHttp
from treasury.settings import TreasurySettings

log = logging.getLogger(__name__)


def futures_equity_usd(http: BlofinHttp) -> float:
    bal = http.get_balance("futures")
    if isinstance(bal, dict):
        try:
            return float(bal.get("totalEquity") or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


def process_new_deposits(
    http: BlofinHttp,
    settings: TreasurySettings,
    seen_ids: list[str],
) -> tuple[list[str], float]:
    """Return updated seen ids and USD credited from new completed deposits."""
    rows = http.get_deposit_history(currency=settings.target_currency, limit=50)
    new_credited = 0.0
    seen = set(seen_ids)
    first_bootstrap = len(seen) == 0

    for row in rows:
        dep_id = str(row.get("depositId") or "")
        if not dep_id:
            continue
        state = str(row.get("state", ""))
        if dep_id in seen:
            continue
        seen.add(dep_id)
        if first_bootstrap or state != "1":
            continue
        try:
            amt = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            continue
        new_credited += amt
        log.info(
            "new deposit %s %s on %s tx=%s",
            amt,
            settings.target_currency,
            row.get("chain"),
            row.get("txId"),
        )

    if first_bootstrap:
        log.info("bootstrapped %d historical deposit ids (no transfer)", len(seen))

    trimmed = list(seen)
    if len(trimmed) > 500:
        trimmed = trimmed[-500:]
    return trimmed, new_credited


def transfer_deposit_to_futures(
    http: BlofinHttp,
    settings: TreasurySettings,
    amount: float,
    *,
    dry_run: bool,
) -> bool:
    if amount <= 0:
        return False
    amt_str = f"{amount:.8f}".rstrip("0").rstrip(".")
    if dry_run:
        log.info(
            "DRY_RUN transfer %s %s funding -> futures",
            amt_str,
            settings.target_currency,
        )
        return True
    try:
        http.asset_transfer(
            currency=settings.target_currency,
            amount=amt_str,
            from_account="funding",
            to_account="futures",
            client_id=f"treasury-{uuid.uuid4().hex[:12]}",
        )
        log.info("transferred %s %s to futures", amt_str, settings.target_currency)
        return True
    except Exception as e:
        log.error("funding->futures transfer failed: %s", e)
        return False


def min_deposit_for_chain(http: BlofinHttp, chain: str, currency: str) -> float:
    for row in http.list_currencies():
        if str(row.get("currency", "")).upper() != currency.upper():
            continue
        chain_name = str(row.get("chain", ""))
        if chain.upper() in chain_name.upper() or chain_name.upper() in chain.upper():
            try:
                return float(row.get("depositMinAmount") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0
