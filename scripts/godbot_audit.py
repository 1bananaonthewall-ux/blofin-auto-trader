#!/usr/bin/env python3
"""
God Bot audit — verify dashboard, stack, modules, and exchange TPSL.

  python scripts/godbot_audit.py           # report only
  python scripts/godbot_audit.py --fix     # repair naked TPSL + write report
  python scripts/godbot_audit.py --dashboard http://127.0.0.1:5050
"""

from __future__ import annotations

import argparse
import compileall
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "state" / "godbot_audit.json"


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, timeout: float = 45.0) -> tuple[int, dict | str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:
        return 0, str(exc)


def audit_compile() -> list[str]:
    issues: list[str] = []
    skip = re.compile(r"[/\\](\.venv|node_modules|__pycache__|dashboard[/\\]dist)[/\\]")
    ok = compileall.compile_dir(str(ROOT), quiet=1, rx=skip)
    if not ok:
        issues.append("python_compile_failed")
    return issues


def audit_imports() -> list[str]:
    issues: list[str] = []
    mods = [
        "bot",
        "core_brain",
        "exchange_client",
        "dashboard_api",
        "dashboard_live",
        "dashboard_copilot",
        "tpsl_guard",
        "self_heal",
        "position_steward",
    ]
    for m in mods:
        try:
            __import__(m)
        except Exception as exc:
            issues.append(f"import_{m}:{exc}")
    return issues


def audit_dashboard(base: str) -> dict:
    out: dict = {"base": base, "checks": {}, "issues": []}
    routes = [
        ("GET", f"{base}/api/health", None),
        ("GET", f"{base}/api/live/snapshot", None),
        ("GET", f"{base}/api/pnl-curve?range=ALL&limit=50", None),
        ("GET", f"{base}/api/scanner?limit=5", None),
        ("GET", f"{base}/api/logs?n=5", None),
        ("GET", f"{base}/api/chat/history", None),
    ]
    for method, url, body in routes:
        code, payload = _http_json(url, method=method, body=body)
        key = url.replace(base, "")
        out["checks"][key] = code
        if code != 200:
            out["issues"].append(f"http_{code}:{key}")
    code, payload = _http_json(f"{base}/api/stack/status", method="POST", body={})
    out["checks"]["/api/stack/status"] = code
    if code != 200:
        out["issues"].append(f"stack_status:{code}")
    else:
        out["stack_output"] = (payload.get("output") if isinstance(payload, dict) else str(payload))[:500]
    snap_code, snap = _http_json(f"{base}/api/live/snapshot")
    if snap_code == 200 and isinstance(snap, dict):
        curve = snap.get("pnl_curve") or {}
        out["pnl_points"] = len(curve.get("equity") or [])
        out["bot_running"] = (snap.get("status") or {}).get("bot_running")
        if out["pnl_points"] < 2:
            out["issues"].append("pnl_curve_empty")
    return out


def audit_tpsl(*, fix: bool) -> dict:
    from config import load_settings
    from exchange_client import BlofinExchange
    from markets import symbol_to_inst_id
    from position_registry import PositionRegistry
    from tpsl_guard import pending_is_adequate
    from whatsapp_agent import is_bot_running

    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    reg = PositionRegistry(settings.state_dir)
    rows: list[dict] = []
    issues: list[str] = []
    repaired: list[str] = []

    for sym, pos in sorted(ex.fetch_all_positions().items()):
        trade = str(pos.get("symbol") or sym).split("#", 1)[0]
        side = str(pos.get("side") or "")
        entry = float(pos.get("entry_price") or 0)
        contracts = float(pos.get("contracts") or 0)
        if not side or entry <= 0 or contracts <= 0:
            continue
        inst = symbol_to_inst_id(trade)
        ps = ex._position_side_for_order(side, pos)
        _, pending = ex._pending_tpsl(
            inst,
            side,
            entry,
            position_side=ps,
            allow_registry_fallback=False,
        )
        ok = pending.live_rows > 0 and pending_is_adequate(side, entry, pending)
        row = {
            "symbol": trade,
            "side": side,
            "live_rows": pending.live_rows,
            "has_sl": pending.has_sl,
            "has_tp": pending.has_tp,
            "issues": list(pending.issues),
            "adequate": ok,
        }
        rows.append(row)
        if not ok:
            issues.append(f"naked:{trade}")
            if fix and not settings.dry_run:
                ex._clear_tpsl_trust(trade)
                ex._tpsl_repair_at.pop(ex._canonical_symbol(trade), None)
                meta = reg.get(trade) or {}
                take = float(meta.get("take_pct") or pos.get("take_pct") or 0.022)
                lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
                placed, _, _ = ex.repair_position_tpsl(
                    trade,
                    side,
                    contracts,
                    take_pct=take,
                    configured_leverage=lev,
                    dry_run=False,
                    cancel_existing=True,
                    registry_meta=meta,
                )
                if placed:
                    repaired.append(trade)
                time.sleep(0.35)

    if fix and repaired:
        time.sleep(1.5)
        issues = []
        for row in rows:
            trade = row["symbol"]
            side = row["side"]
            pos = ex._lookup_open_position(trade, side)
            if not pos:
                row["after_adequate"] = False
                issues.append(f"closed:{trade}")
                continue
            entry = float(pos.get("entry_price") or 0)
            inst = symbol_to_inst_id(trade)
            ps = ex._position_side_for_order(side, pos)
            _, pending = ex._pending_tpsl(
                inst, side, entry, position_side=ps, allow_registry_fallback=False
            )
            row["after_live_rows"] = pending.live_rows
            row["after_adequate"] = pending.live_rows > 0 and pending_is_adequate(
                side, entry, pending
            )
            if not row["after_adequate"]:
                issues.append(f"naked:{trade}")
                if not settings.dry_run:
                    ex._clear_tpsl_trust(trade)
                    meta = reg.get(trade) or {}
                    take = float(meta.get("take_pct") or 0.022)
                    lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
                    ex.repair_position_tpsl(
                        trade,
                        side,
                        float(pos.get("contracts") or 0),
                        take_pct=take,
                        configured_leverage=lev,
                        dry_run=False,
                        cancel_existing=True,
                        registry_meta=meta,
                    )
                    time.sleep(0.4)
                    _, pending2 = ex._pending_tpsl(
                        inst, side, entry, position_side=ps, allow_registry_fallback=False
                    )
                    row["after_live_rows"] = pending2.live_rows
                    row["after_adequate"] = pending2.live_rows > 0 and pending_is_adequate(
                        side, entry, pending2
                    )
                    if not row["after_adequate"]:
                        issues.append(f"still_naked:{trade}")

    return {
        "bot_running": is_bot_running(),
        "dry_run": settings.dry_run,
        "open_count": len(rows),
        "positions": rows,
        "issues": issues,
        "repaired": repaired,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="God Bot system audit")
    ap.add_argument("--fix", action="store_true", help="Repair missing exchange TPSL")
    ap.add_argument("--dashboard", default="http://127.0.0.1:5050")
    ap.add_argument("--skip-dashboard", action="store_true")
    args = ap.parse_args()

    report: dict = {"ts": time.time(), "sections": {}}
    all_issues: list[str] = []

    report["sections"]["compile"] = audit_compile()
    all_issues.extend(report["sections"]["compile"])

    report["sections"]["imports"] = audit_imports()
    all_issues.extend(report["sections"]["imports"])

    if not args.skip_dashboard:
        report["sections"]["dashboard"] = audit_dashboard(args.dashboard.rstrip("/"))
        all_issues.extend(report["sections"]["dashboard"].get("issues") or [])

    report["sections"]["tpsl"] = audit_tpsl(fix=args.fix)
    all_issues.extend(report["sections"]["tpsl"].get("issues") or [])

    report["ok"] = len(all_issues) == 0
    report["issue_count"] = len(all_issues)
    report["issues"] = all_issues

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"\nReport: {REPORT_PATH}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
