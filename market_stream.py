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

import api_backoff
from api_backoff import RateLimitPaused, parse_retry_after, register_429
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
        self._ws_app: Any = None
        self._candle_subscribed: set[str] = set()
        self._ws_rot_offset = 0
        self._ticker_backoff_until = 0.0
        self._ticker_backoff_sec = 90.0
        self._last_429_log = 0.0

    def start(self, inst_ids: list[str]) -> None:
        with self._lock:
            self._inst_ids = list(inst_ids)
        if not api_backoff.is_paused():
            self.refresh_all_tickers(force=True)
        else:
            log.warning(
                "API paused — skipping startup ticker refresh (%.0fs left)",
                api_backoff.seconds_left(),
            )
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
        self._subscribe_priority_candles()

    def _subscribe_priority_candles(self) -> None:
        """Live WS subscribe for open positions + scan leaders (no full reconnect)."""
        ws = self._ws_app
        if ws is None:
            return
        try:
            sock = getattr(ws, "sock", None)
            if sock is None or not sock.connected:
                return
        except Exception:
            return
        with self._lock:
            pri = list(self._priority)[:80]
        args: list[dict] = []
        for iid in pri:
            if iid in self._candle_subscribed:
                continue
            args.extend(
                [
                    {"channel": "tickers", "instId": iid},
                    {"channel": "candle1m", "instId": iid},
                    {"channel": "candle5m", "instId": iid},
                ]
            )
            self._candle_subscribed.add(iid)
        if args:
            self._subscribe(ws, args)

    def refresh_all_tickers(self, *, force: bool = False) -> int:
        now = time.time()
        if api_backoff.is_paused():
            with self._lock:
                return len(self.tickers)
        if not force and now < self._ticker_backoff_until:
            with self._lock:
                return len(self.tickers)
        try:
            rows = self.http.list_tickers()
            with self._lock:
                for row in rows:
                    iid = row.get("instId") or ""
                    if iid:
                        self.tickers[iid] = row
                self._last_rest_tick = time.time()
            self._ticker_backoff_sec = 90.0
            return len(rows)
        except RateLimitPaused:
            self._sync_global_ticker_backoff(now)
            with self._lock:
                return len(self.tickers)
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg or "1015" in msg:
                self._sync_global_ticker_backoff(now, source="REST tickers")
            else:
                log.warning("ticker refresh failed: %s", msg[:160])
            with self._lock:
                return len(self.tickers)

    def _sync_global_ticker_backoff(self, now: float, *, source: str = "REST tickers") -> None:
        register_429(None, source=source)
        global_left = api_backoff.seconds_left()
        self._ticker_backoff_until = max(self._ticker_backoff_until, now + global_left)
        self._ticker_backoff_sec = min(600.0, max(self._ticker_backoff_sec * 1.5, global_left))
        if now - self._last_429_log > 120.0:
            log.warning(
                "ticker refresh rate limited — REST backoff %.0fs (using %d cached tickers)",
                self._ticker_backoff_until - now,
                len(self.tickers),
            )
            self._last_429_log = now

    def _rest_ticker_loop(self) -> None:
        while True:
            now = time.time()
            if api_backoff.is_paused():
                sleep_s = max(60.0, api_backoff.seconds_left())
                time.sleep(sleep_s)
                continue
            health = self.stream_health()
            ws_live = bool(health.get("ws_live"))
            ticker_age = float(health.get("ticker_age_sec") or 999.0)
            # WS feeds priority tickers — REST is fallback only.
            if now >= self._ticker_backoff_until:
                if not ws_live or ticker_age > 180.0:
                    self.refresh_all_tickers()
            if now < self._ticker_backoff_until:
                sleep_s = max(30.0, self._ticker_backoff_until - now)
            elif ws_live and ticker_age < 120.0:
                sleep_s = 240.0
            elif ws_live:
                sleep_s = 150.0
            else:
                sleep_s = 120.0
            time.sleep(sleep_s)

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
        """Rank symbols by 24h move + volume, preferring steady directional runners over chop."""
        from run_quality import _path_efficiency

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
                    run_mult = 0.72
                    buf = self.candles_1m.get(iid)
                    if buf and len(buf) >= 40:
                        closes = [float(c[4]) for c in buf]
                        pe = _path_efficiency(closes, 45)
                        run_mult = 0.40 + 0.60 * pe
                    score = (chg * math.log1p(vol) + math.log1p(last) * 0.02) * run_mult
                    scored.append((score, inst_id_to_symbol(iid)))
                except (TypeError, ValueError):
                    continue
        scored.sort(reverse=True)
        limit = top_n if top_n is not None else len(scored)
        return [s for _, s in scored[:limit]]

    def _ws_on_error(self, _ws, err: Exception | str) -> None:
        msg = str(err)
        if "429" in msg or "1015" in msg:
            self._ws_disabled = True
            retry = parse_retry_after(None, 429, msg)
            register_429(retry, source="WebSocket")
            self._ticker_backoff_until = max(
                self._ticker_backoff_until,
                time.time() + api_backoff.seconds_left(),
            )
            if not self._ws_warned:
                self._ws_warned = True
                log.warning(
                    "WebSocket rate limited (429) — disabled until backoff clears (%.0fs)",
                    api_backoff.seconds_left(),
                )
            return
        if "403" in msg or "Forbidden" in msg or "cloudflare" in msg.lower():
            self._ws_disabled = True
            if not self._ws_warned:
                self._ws_warned = True
                log.warning(
                    "WebSocket blocked (Cloudflare) — REST ticker fallback every ~2min (429-aware)"
                )
            return
        if not self._ws_warned:
            log.warning("ws error: %s", err)

    def _ws_loop(self) -> None:
        while self._ws_running:
            if api_backoff.is_paused():
                time.sleep(max(600.0, api_backoff.seconds_left()))
                continue
            if self._ws_disabled:
                time.sleep(max(600.0, api_backoff.seconds_left(), 60.0))
                if not api_backoff.is_paused():
                    self._ws_disabled = False
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
        self._ws_app = ws
        with self._lock:
            self._candle_subscribed.clear()
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
            self._candle_subscribed.add(iid)
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
