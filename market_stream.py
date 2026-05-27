"""
Real-time Blofin market hub — REST snapshot of all tickers + WebSocket streams.

- Instant universe: REST /market/tickers (all instruments) refreshed continuously
- WebSocket: live ticker + 1m/5m candles for priority symbols (open positions + top movers)
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from typing import Any

from blofin_http import BlofinHttp
from markets import inst_id_to_symbol, symbol_to_inst_id

log = logging.getLogger(__name__)

WS_PUBLIC = "wss://openapi.blofin.com/ws/public"
WS_DEMO = "wss://demo-trading-openapi.blofin.com/ws/public"

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore


def _parse_candle_row(row: list) -> list[float]:
    return [
        float(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]) if len(row) > 5 else 0.0,
    ]


class BlofinMarketStream:
    def __init__(self, http: BlofinHttp, *, demo: bool = False) -> None:
        self.http = http
        self.url = WS_DEMO if demo else WS_PUBLIC
        self._lock = threading.RLock()
        self.tickers: dict[str, dict[str, Any]] = {}
        self.candles_1m: dict[str, deque] = {}
        self.candles_5m: dict[str, deque] = {}
        self._inst_ids: list[str] = []
        self._priority: set[str] = set()
        self._ws_running = False
        self._ws_disabled = False
        self._ws_warned = False
        self._ticker_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._last_rest_tick = 0.0

    def start(self, inst_ids: list[str]) -> None:
        with self._lock:
            self._inst_ids = list(inst_ids)
        self.refresh_all_tickers()
        self._ticker_thread = threading.Thread(target=self._rest_ticker_loop, daemon=True)
        self._ticker_thread.start()
        if websocket is None:
            log.warning("websocket-client not installed — REST-only tickers (pip install websocket-client)")
            return
        self._ws_running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()
        log.info("market stream started | %d instruments | ws=%s", len(inst_ids), self.url)

    def stop(self) -> None:
        self._ws_running = False

    def set_priority(self, inst_ids: list[str]) -> None:
        with self._lock:
            self._priority = set(inst_ids)

    def refresh_all_tickers(self) -> int:
        try:
            rows = self.http.list_tickers()
            with self._lock:
                for row in rows:
                    iid = row.get("instId") or ""
                    if iid:
                        self.tickers[iid] = row
                self._last_rest_tick = time.time()
            return len(rows)
        except Exception:
            log.exception("ticker refresh failed")
            return 0

    def _rest_ticker_loop(self) -> None:
        while True:
            self.refresh_all_tickers()
            time.sleep(12)

    def get_last_price(self, symbol: str) -> float | None:
        iid = symbol_to_inst_id(symbol)
        with self._lock:
            row = self.tickers.get(iid)
        if not row:
            return None
        try:
            return float(row.get("last") or row.get("lastPrice") or 0) or None
        except (TypeError, ValueError):
            return None

    def get_ticker(self, symbol: str) -> dict[str, Any] | None:
        iid = symbol_to_inst_id(symbol)
        with self._lock:
            return self.tickers.get(iid)

    def bootstrap_candles(self, symbol: str, bar: str = "1m", limit: int = 80) -> None:
        iid = symbol_to_inst_id(symbol)
        try:
            raw = self.http.get_candles(iid, bar=bar, limit=limit)
            parsed = [_parse_candle_row(r) for r in raw if len(r) >= 5]
            if not parsed:
                return
            with self._lock:
                target = self.candles_1m if bar == "1m" else self.candles_5m
                target[iid] = deque(parsed[-120:], maxlen=120)
        except Exception:
            log.debug("bootstrap candles failed %s", symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1m", min_bars: int = 40) -> list[list[float]] | None:
        iid = symbol_to_inst_id(symbol)
        with self._lock:
            buf = self.candles_1m.get(iid) if timeframe == "1m" else self.candles_5m.get(iid)
            if buf and len(buf) >= min_bars:
                return list(buf)
        return None

    def stream_health(self) -> dict[str, float | bool | int]:
        """Signals for adaptive scan depth (REST freshness + WS + coverage)."""
        with self._lock:
            n_inst = len(self._inst_ids)
            n_tickers = sum(1 for i in self._inst_ids if i in self.tickers)
            n_candles = len(self.candles_1m)
            age = time.time() - self._last_rest_tick if self._last_rest_tick else 999.0
        return {
            "universe_n": n_inst,
            "ticker_count": n_tickers,
            "ticker_coverage": n_tickers / max(n_inst, 1),
            "ticker_age_sec": age,
            "ws_live": bool(self._ws_running and not self._ws_disabled),
            "candle_symbols": n_candles,
        }

    def momentum_rank(self, inst_ids: list[str], top_n: int | None = None) -> list[str]:
        """Rank symbols by 24h activity for ML scan priority."""
        scored: list[tuple[float, str]] = []
        with self._lock:
            for iid in inst_ids:
                row = self.tickers.get(iid)
                if not row:
                    continue
                try:
                    last = float(row.get("last") or 0)
                    open24 = float(row.get("open24h") or last)
                    vol = float(row.get("vol24h") or row.get("volCurrency24h") or 0)
                    if last <= 0:
                        continue
                    chg = abs((last - open24) / open24) if open24 else 0
                    score = chg * math.log1p(vol) + math.log1p(last)
                    scored.append((score, inst_id_to_symbol(iid)))
                except (TypeError, ValueError):
                    continue
        scored.sort(reverse=True)
        limit = top_n if top_n is not None else len(scored)
        return [s for _, s in scored[:limit]]

    def _ws_on_error(self, _ws, err: Exception | str) -> None:
        msg = str(err)
        if "403" in msg or "Forbidden" in msg or "cloudflare" in msg.lower():
            self._ws_disabled = True
            if not self._ws_warned:
                self._ws_warned = True
                log.warning(
                    "WebSocket blocked (Cloudflare) — using REST ticker refresh only (every ~12s)"
                )
            return
        if not self._ws_warned:
            log.warning("ws error: %s", err)

    def _ws_loop(self) -> None:
        while self._ws_running:
            if self._ws_disabled:
                time.sleep(30)
                continue
            try:
                ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._ws_on_error,
                    on_close=lambda _w, *_: None,
                )
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception:
                if not self._ws_disabled:
                    log.debug("ws loop error", exc_info=True)
            time.sleep(5)

    def _on_open(self, ws) -> None:
        with self._lock:
            priority = list(self._priority)
            all_ids = list(self._inst_ids)
        rot_start = getattr(self, "_ws_rot_offset", 0) % max(len(all_ids), 1)
        rot_batch = min(80, max(40, len(all_ids) // 12))
        for iid in priority[:min(80, len(priority))]:
            self._subscribe(ws, [
                {"channel": "tickers", "instId": iid},
                {"channel": "candle1m", "instId": iid},
                {"channel": "candle5m", "instId": iid},
            ])
        batch = 0
        for j in range(rot_batch):
            iid = all_ids[(rot_start + j) % len(all_ids)]
            if iid in self._priority:
                continue
            self._subscribe(ws, [{"channel": "tickers", "instId": iid}])
            batch += 1
        self._ws_rot_offset = (rot_start + rot_batch) % max(len(all_ids), 1)
        log.info("ws subscribed priority=%d rot_tickers=%d offset=%d", len(priority), batch, rot_start)

    def set_ws_rotation_offset(self, offset: int) -> None:
        self._ws_rot_offset = max(0, offset)

    def _subscribe(self, ws, args: list[dict]) -> None:
        try:
            ws.send(json.dumps({"op": "subscribe", "args": args}))
            time.sleep(0.08)
        except Exception:
            pass

    def _on_message(self, _ws, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        if msg.get("event") in ("subscribe", "unsubscribe", "error"):
            if msg.get("event") == "error":
                log.debug("ws: %s", msg.get("msg"))
            return
        arg = msg.get("arg") or {}
        channel = arg.get("channel") or ""
        inst_id = arg.get("instId") or ""
        data = msg.get("data")
        if not inst_id or not data:
            return
        if channel == "tickers":
            row = data[0] if isinstance(data, list) else data
            with self._lock:
                self.tickers[inst_id] = row
            return
        if channel.startswith("candle"):
            rows = data if isinstance(data, list) else [data]
            parsed = [_parse_candle_row(r) for r in rows if isinstance(r, list) and len(r) >= 5]
            if not parsed:
                return
            with self._lock:
                buf = self.candles_1m if "1m" in channel else self.candles_5m
                if inst_id not in buf:
                    buf[inst_id] = deque(maxlen=120)
                for bar in parsed:
                    ts = bar[0]
                    if buf[inst_id] and buf[inst_id][-1][0] == ts:
                        buf[inst_id][-1] = bar
                    else:
                        buf[inst_id].append(bar)
