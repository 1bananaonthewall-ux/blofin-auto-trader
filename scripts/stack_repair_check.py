#!/usr/bin/env python3
"""JSON stack readiness probe for repair loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _dashboard_listening(port: int = 5050) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=4) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    from whatsapp_agent import is_bot_running

    bot = is_bot_running()
    dash = _dashboard_listening()
    payload = {
        "ready": bot and dash,
        "bot_running": bot,
        "dashboard_listening": dash,
    }
    print(json.dumps(payload))
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
