from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

_STABLE = {"USDT", "USDC", "USD", "DAI", "BUSD", "TUSD", "FDUSD"}
_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 120.0


def usd_price(symbol: str) -> float:
    sym = symbol.upper()
    if sym in _STABLE:
        return 1.0
    now = time.time()
    cached = _CACHE.get(sym)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": _coingecko_id(sym), "vs_currencies": "usd"},
            timeout=15,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        cid = _coingecko_id(sym)
        price = float(data.get(cid, {}).get("usd", 0) or 0)
        if price > 0:
            _CACHE[sym] = (price, now)
            return price
    except Exception as e:
        log.debug("price fetch failed for %s: %s", sym, e)
    return cached[0] if cached else 0.0


def _coingecko_id(symbol: str) -> str:
    mapping = {
        "ETH": "ethereum",
        "BTC": "bitcoin",
        "BNB": "binancecoin",
        "MATIC": "matic-network",
        "POL": "matic-network",
        "TRX": "tron",
        "SOL": "solana",
        "ARB": "arbitrum",
        "OP": "optimism",
        "AVAX": "avalanche-2",
        "DOGE": "dogecoin",
    }
    return mapping.get(symbol.upper(), symbol.lower())
