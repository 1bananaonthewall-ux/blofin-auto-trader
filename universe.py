from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import api_backoff
from leverage_intel import parse_instrument_max_leverage
from markets import Market, inst_id_to_symbol

if TYPE_CHECKING:
    from exchange_client import BlofinExchange

log = logging.getLogger(__name__)


def _cached_tradeable_markets(ex: "BlofinExchange") -> list[Market]:
    if ex.markets:
        return list(ex.markets.values())
    cached = ex.load_markets_from_cache(ex.settings.state_dir)
    if cached:
        ex.markets = cached
        log.info("using %d markets from disk cache (API paused)", len(cached))
        return list(cached.values())
    return []


def load_tradeable_markets(
    ex: "BlofinExchange",
    equity: float,
    leverage: int,
    margin_utilization: float,
    max_positions_cap: int = 9999,
) -> list[Market]:
    """Load ALL tradeable USDT markets. Filter only dead instruments.
    Tiny accounts get everything passed through - let the sizer decide."""
    if api_backoff.is_paused():
        return _cached_tradeable_markets(ex)

    instruments = ex.list_instruments()
    tickers = {t["instId"]: t for t in ex.list_tickers()}

    markets: list[Market] = []
    for inst in instruments:
        inst_id = inst.get("instId") or ""
        if not inst_id.endswith("-USDT"):
            continue
        state = (inst.get("state") or inst.get("status") or "live").lower()
        if state not in ("live", "trading", ""):
            continue

        ticker = tickers.get(inst_id)
        if not ticker:
            continue
        price = float(ticker.get("last") or ticker.get("lastPrice") or 0)
        if price <= 0:
            continue

        min_size = float(inst.get("minSize") or inst.get("lotSize") or 0)
        contract_size = float(inst.get("contractValue") or inst.get("ctVal") or 1)
        if min_size <= 0:
            continue

        inst_max = parse_instrument_max_leverage(inst) or leverage
        market = Market(
            inst_id=inst_id,
            symbol=inst_id_to_symbol(inst_id),
            min_size=min_size,
            contract_size=contract_size,
            last_price=price,
            max_leverage=min(inst_max, leverage) if leverage else inst_max,
        )

        # Extremely permissive filter: only reject if min_notional > 90% of equity
        min_margin = market.min_margin_usdt / max(leverage, 1)
        if equity > 0 and min_margin > equity * 0.90:
            continue

        markets.append(market)

    # Sort cheapest first
    markets.sort(key=lambda m: m.min_margin_usdt)
    return markets


def training_symbol_cap(settings) -> int:
    """0 = every live USDT perp on the exchange; >0 caps count (legacy)."""
    all_univ = getattr(settings, "trade_all_symbols", None)
    if all_univ is None:
        tu = getattr(settings, "trade_universe", "").strip().lower()
        all_univ = tu in {"all", "*", "universe"}
    if all_univ:
        return 0
    raw = int(getattr(settings, "ml_train_symbols", 0) or 0)
    return 0 if raw <= 0 else raw


def load_training_markets(ex: "BlofinExchange", cap: int = 0) -> list[Market]:
    """All live USDT perps for ML training — not filtered by account equity. cap<=0 = full exchange."""
    instruments = ex.list_instruments()
    tickers = {t["instId"]: t for t in ex.list_tickers()}
    markets: list[Market] = []
    for inst in instruments:
        inst_id = inst.get("instId") or ""
        if not inst_id.endswith("-USDT"):
            continue
        state = (inst.get("state") or inst.get("status") or "live").lower()
        if state not in ("live", "trading", ""):
            continue
        ticker = tickers.get(inst_id)
        if not ticker:
            continue
        price = float(ticker.get("last") or ticker.get("lastPrice") or 0)
        if price <= 0:
            continue
        min_size = float(inst.get("minSize") or inst.get("lotSize") or 0)
        contract_size = float(inst.get("contractValue") or inst.get("ctVal") or 1)
        if min_size <= 0:
            continue
        vol = float(ticker.get("vol24h") or ticker.get("volCurrency24h") or 0)
        markets.append(
            (
                vol,
                Market(
                    inst_id=inst_id,
                    symbol=inst_id_to_symbol(inst_id),
                    min_size=min_size,
                    contract_size=contract_size,
                    last_price=price,
                ),
            )
        )
    markets.sort(key=lambda x: x[0], reverse=True)
    if cap <= 0:
        return [m for _, m in markets]
    return [m for _, m in markets[:cap]]