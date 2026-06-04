#!/usr/bin/env python3
"""Smoke-test dashboard REST + live hub + WebSocket."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dashboard_api  # noqa: E402 — loads app + binds helpers
from dashboard_live import get_live_hub


def main() -> int:
    hub = get_live_hub()
    time.sleep(3)
    snap = hub.get_snapshot()
    assert snap.get("stream_ts"), "missing stream_ts"
    assert "status" in snap, "missing status"
    assert "positions" in snap, "missing positions"
    assert "active_setups" in snap, "missing active_setups"
    assert "closed" in snap, "missing closed"
    assert "scanner" in snap, "missing scanner"
    assert "pnl_curve" in snap, "missing pnl_curve"
    assert "log_tail" in snap, "missing log_tail"
    print("live hub ok:", snap["stream_ts"])
    print("  setups:", len(snap["active_setups"]), "positions:", len(snap["positions"]))
    print("  closed:", len(snap["closed"]), "scanner picks:", snap["scanner"]["count"])
    print("  log lines:", len(snap["log_tail"]))

    client = dashboard_api.app.test_client()
    for path in ("/api/health", "/api/scanner?limit=3", "/api/logs?n=5"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        print(f"GET {path} -> 200")

    try:
        from simple_websocket import Client as SimpleWSClient
        from werkzeug.serving import make_server
        import threading

        server = make_server("127.0.0.1", 5059, dashboard_api.app, threaded=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.5)
        ws = SimpleWSClient("ws://127.0.0.1:5059/ws/live")
        hello = json.loads(ws.receive())
        assert hello.get("type") == "hello", hello
        assert hello.get("data"), "hello should include initial snapshot"
        msg = json.loads(ws.receive())
        assert msg.get("type") in ("update", "heartbeat"), msg
        ws.close()
        server.shutdown()
        print("websocket ok:", msg.get("type"))
    except ImportError:
        print("websocket test skipped (simple-websocket)")

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
