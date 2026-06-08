"""Cached OHLCV loads for backtests."""

from __future__ import annotations

import logging
from typing import Any

from storefront_market import fetch_candles_history

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[list[list[float]], list[list[float]]]] = {}


def load_symbol_candles(
    inst_id: str,
    *,
    start_ms: int,
    end_ms: int,
    warmup_5m: int = 120,
    lookback_1h: int = 60,
) -> tuple[list[list[float]], list[list[float]]]:
    key = f"{inst_id}:{start_ms}:{end_ms}"
    if key in _CACHE:
        return _CACHE[key]
    candles_5m = fetch_candles_history(
        inst_id,
        bar="5m",
        start_ms=start_ms - warmup_5m * 300_000,
        end_ms=end_ms,
    )
    candles_1h = fetch_candles_history(
        inst_id,
        bar="1H",
        start_ms=start_ms - lookback_1h * 3_600_000,
        end_ms=end_ms,
    )
    _CACHE[key] = (candles_5m, candles_1h)
    log.info("%s loaded %d 5m / %d 1H bars", inst_id, len(candles_5m), len(candles_1h))
    return candles_5m, candles_1h


def clear_candle_cache() -> None:
    _CACHE.clear()
