#!/usr/bin/env python3
"""Smoke-test dashboard API + copilot after stack restart."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5050"


def get(path: str, timeout: int = 15) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(f"{BASE}{path}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def post_chat(message: str, timeout: int = 120) -> tuple[int, str]:
    payload = json.dumps({"message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, str(data.get("reply", ""))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def main() -> int:
    fails = 0
    checks: list[tuple[str, bool, str]] = []

    code, status = get("/api/status", timeout=8)
    ok = code == 200 and isinstance(status, dict)
    checks.append(("status", ok, f"http={code}"))
    if ok:
        checks.append(("bot_running", bool(status.get("bot_running")), str(status.get("bot_running"))))

    code, health = get("/api/health")
    checks.append(("health", code == 200, f"http={code}"))

    code, scanner = get("/api/scanner?limit=8")
    ok = code == 200 and isinstance(scanner, dict)
    picks = len(scanner.get("picks") or scanner.get("rows") or []) if ok else 0
    checks.append(("scanner", ok, f"picks={picks}"))

    code, logs = get("/api/logs?n=40")
    ok = code == 200 and isinstance(logs, dict)
    lines = len(logs.get("lines") or logs.get("entries") or []) if ok else 0
    checks.append(("logs", ok, f"lines={lines}"))

    code, hist = get("/api/chat/history")
    checks.append(("chat_history", code == 200, f"http={code}"))

    print("=== API checks ===")
    for name, passed, detail in checks:
        mark = "OK" if passed else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
        if not passed:
            fails += 1

    print("\n=== Copilot ping (may take up to 90s on first HF load) ===")
    t0 = time.time()
    code, reply = post_chat("status check - how are we doing?", timeout=120)
    elapsed = time.time() - t0
    legacy = "only outputs JSON" in (reply or "")
    cop_ok = code == 200 and reply and not legacy and len(reply) > 40
    print(f"  [{'OK' if cop_ok else 'FAIL'}] chat http={code} len={len(reply or '')} t={elapsed:.1f}s")
    if legacy:
        print("  FAIL: legacy HF blocker message still returned")
        fails += 1
    elif not cop_ok:
        fails += 1
        print(f"  reply preview: {(reply or '')[:300]}")
    else:
        print(f"  preview: {reply[:280]}...")

    print("\n=== LLM provider ===")
    try:
        from config import load_settings

        load_settings()
        from local_llm import resolve_provider, status_line

        print(f"  provider={resolve_provider()} | {status_line()}")
    except Exception as exc:
        print(f"  WARN: {exc}")

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
