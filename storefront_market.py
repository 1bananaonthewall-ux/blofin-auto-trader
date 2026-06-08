"""Public Blofin market data for Bob's Bots backtests (no API keys required)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from markets import inst_id_to_symbol

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.blofin.com"
_SESSION = requests.Session()
ROOT = Path(__file__).resolve().parent
MARKETS_CACHE = ROOT / "state" / "markets_cache.json"
_BAR_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}


def _public_get(path: str, params: dict[str, Any] | None = None, *, retries: int = 5) -> Any:
    url = BASE_URL + path
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, params=params or {}, timeout=25)
            if resp.status_code == 429:
                wait = min(60.0, 2.0 ** attempt * 3.0)
                log.warning("Blofin 429 on %s — backoff %.0fs", path, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            code = str(body.get("code", ""))
            if code not in ("0", "200", ""):
                raise RuntimeError(f"Blofin market error {code}: {body.get('msg', body)}")
            return body.get("data")
        except requests.HTTPError as exc:
            last_exc = exc
            if exc.response is not None and exc.response.status_code == 429:
                wait = min(60.0, 2.0 ** attempt * 3.0)
                time.sleep(wait)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            time.sleep(1.0 + attempt)
    if last_exc:
        raise last_exc
    return None


def tradingview_symbol(inst_id: str) -> str:
    """TradingView perpetual symbol — BLOFIN first, Binance perp fallback."""
    base = inst_id.replace("-USDT", "").upper()
    return f"BLOFIN:{base}USDT.P"


def tradingview_symbol_fallback(inst_id: str) -> str:
    base = inst_id.replace("-USDT", "").upper()
    return f"BINANCE:{base}USDT.P"


def _assets_from_disk_cache(*, min_price: float = 0.0) -> list[dict[str, Any]] | None:
    if not MARKETS_CACHE.is_file():
        return None
    try:
        raw = json.loads(MARKETS_CACHE.read_text(encoding="utf-8"))
        markets = raw.get("markets") or raw
        items: list[dict[str, Any]] = []
        if isinstance(markets, list):
            items = markets
        elif isinstance(markets, dict):
            for inst_id, m in markets.items():
                if isinstance(m, dict):
                    items.append({**m, "inst_id": m.get("inst_id") or inst_id})
        rows: list[dict[str, Any]] = []
        for m in items:
            inst_id = str(m.get("inst_id") or "")
            if not inst_id.endswith("-USDT"):
                continue
            last = float(m.get("last_price") or m.get("last") or 0)
            if last < min_price:
                continue
            base = inst_id.replace("-USDT", "")
            sym = m.get("symbol") or inst_id_to_symbol(inst_id)
            rows.append(
                {
                    "inst_id": inst_id,
                    "symbol": sym,
                    "base": base,
                    "last": last,
                    "vol24h": float(m.get("vol24h") or 0),
                    "chg24_pct": 0.0,
                    "tradingview": tradingview_symbol(inst_id),
                    "tradingview_fallback": tradingview_symbol_fallback(inst_id),
                }
            )
        rows.sort(key=lambda r: r["vol24h"], reverse=True)
        if rows:
            log.info("using %d assets from markets_cache.json (API backoff)", len(rows))
        return rows or None
    except Exception as exc:
        log.debug("markets_cache read failed: %s", exc)
        return None


def list_tradeable_assets(*, min_price: float = 0.0) -> list[dict[str, Any]]:
    try:
        instruments = _public_get("/api/v1/market/instruments") or []
        tickers = {t["instId"]: t for t in (_public_get("/api/v1/market/tickers") or [])}
    except Exception as exc:
        log.warning("list_tradeable_assets API failed: %s — using disk cache", exc)
        cached = _assets_from_disk_cache(min_price=min_price)
        if cached:
            return cached
        raise
    rows: list[dict[str, Any]] = []
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
        try:
            last = float(ticker.get("last") or ticker.get("lastPrice") or 0)
            vol = float(ticker.get("vol24h") or ticker.get("volCurrency24h") or 0)
            chg = float(ticker.get("change24h") or ticker.get("changePercent24h") or 0)
        except (TypeError, ValueError):
            continue
        if last < min_price:
            continue
        sym = inst_id_to_symbol(inst_id)
        base = inst_id.replace("-USDT", "")
        rows.append(
            {
                "inst_id": inst_id,
                "symbol": sym,
                "base": base,
                "last": last,
                "vol24h": vol,
                "chg24_pct": round(chg * 100, 2) if abs(chg) < 5 else round(chg, 2),
                "tradingview": tradingview_symbol(inst_id),
                "tradingview_fallback": tradingview_symbol_fallback(inst_id),
            }
        )
    rows.sort(key=lambda r: r["vol24h"], reverse=True)
    return rows


def _parse_candle(row: list) -> list[float] | None:
    if len(row) < 5:
        return None
    try:
        return [
            float(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]) if len(row) > 5 else 0.0,
        ]
    except (TypeError, ValueError):
        return None


def fetch_candles_history(
    inst_id: str,
    *,
    bar: str = "4H",
    start_ms: int,
    end_ms: int | None = None,
    page_limit: int = 1440,
) -> list[list[float]]:
    """Paginate Blofin candles oldest-first between start_ms and end_ms."""
    end_ms = end_ms or int(time.time() * 1000)
    bar_ms = _BAR_MS.get(bar, _BAR_MS["4H"])
    needed = max(10, int((end_ms - start_ms) / bar_ms) + 2)
    collected: dict[int, list[float]] = {}
    after: str | None = None
    pages = 0
    max_pages = max(5, (needed // page_limit) + 4)

    while pages < max_pages:
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(min(page_limit, 1440))}
        if after:
            params["after"] = after
        raw = _public_get("/api/v1/market/candles", params) or []
        if not raw:
            break
        pages += 1
        oldest_ts = None
        for row in raw:
            candle = _parse_candle(row)
            if not candle:
                continue
            ts = int(candle[0])
            if ts < start_ms or ts > end_ms:
                continue
            collected[ts] = candle
            oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
        if oldest_ts is None:
            break
        if oldest_ts <= start_ms:
            break
        next_after = str(oldest_ts)
        if next_after == after:
            break
        after = next_after
        time.sleep(0.05)

    return [collected[k] for k in sorted(collected.keys())]
