"""WebSocket tail sync — freshens recent candles while REST loads deep history."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

WS_PUBLIC = "wss://openapi.blofin.com/ws/public"

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore


def _parse_row(row: list) -> list[float] | None:
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


def sync_ws_tails(
    inst_ids: list[str],
    *,
    timeout_sec: float = 20.0,
    max_symbols: int = 80,
) -> dict[str, dict[str, list[list[float]]]]:
    """Short WS burst to capture freshest bars (parallel to REST cache build)."""
    if websocket is None or not inst_ids:
        if websocket is None:
            log.info("websocket-client not installed — skip WS tail sync")
        return {}

    channel_map = {"5m": "candle5m", "1H": "candle1H"}
    targets = inst_ids[:max_symbols]
    store: dict[str, dict[str, list[list[float]]]] = {}
    lock = threading.Lock()
    stop_at = time.time() + timeout_sec

    def on_message(_ws: Any, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        if msg.get("event") in ("subscribe", "error", "pong"):
            return
        arg = msg.get("arg") or {}
        ch = arg.get("channel") or ""
        iid = arg.get("instId") or ""
        if not iid or "candle" not in ch:
            return
        bar = "5m" if ch == "candle5m" else "1H"
        data = msg.get("data")
        rows = data if isinstance(data, list) else []
        if rows and isinstance(rows[0], list):
            batch = rows
        else:
            batch = [rows] if rows else []
        with lock:
            bucket = store.setdefault(iid, {})
            lst = bucket.setdefault(bar, [])
            for raw in batch:
                if isinstance(raw, list):
                    c = _parse_row(raw)
                else:
                    c = None
                if c:
                    lst.append(c)

    def on_open(ws: Any) -> None:
        args = []
        for iid in targets:
            for bar, ch in channel_map.items():
                args.append({"channel": ch, "instId": iid})
        for i in range(0, len(args), 40):
            ws.send(json.dumps({"op": "subscribe", "args": args[i : i + 40]}))

    app = websocket.WebSocketApp(
        WS_PUBLIC,
        on_open=on_open,
        on_message=on_message,
        on_error=lambda _ws, err: log.debug("ws tail: %s", err),
    )
    thread = threading.Thread(
        target=lambda: app.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    thread.start()
    while time.time() < stop_at and thread.is_alive():
        time.sleep(0.5)
    try:
        app.close()
    except Exception:
        pass
    thread.join(timeout=3.0)
    return store


def sync_ws_tails_batched(
    inst_ids: list[str],
    *,
    batch_size: int = 80,
    timeout_sec: float = 10.0,
) -> dict[str, dict[str, list[list[float]]]]:
    """WS tail sync in chunks — freshens recent bars while REST/cache loads history."""
    if not inst_ids:
        return {}
    merged: dict[str, dict[str, list[list[float]]]] = {}
    n_batches = (len(inst_ids) + batch_size - 1) // batch_size
    for bi, i in enumerate(range(0, len(inst_ids), batch_size)):
        chunk = inst_ids[i : i + batch_size]
        log.info("ws tail batch %d/%d (%d symbols)", bi + 1, n_batches, len(chunk))
        merged.update(sync_ws_tails(chunk, timeout_sec=timeout_sec, max_symbols=batch_size))
    return merged
