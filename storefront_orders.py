"""Bob's Bots — order creation, fulfillment, refunds."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state" / "storefront"
ORDERS_FILE = STATE_DIR / "orders.json"
REFUNDS_FILE = STATE_DIR / "refunds.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        return {"orders": [], "refunds": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"orders": [], "refunds": []}


def _save_orders(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ORDERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _save_refunds(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REFUNDS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_orders(email: str | None = None) -> list[dict[str, Any]]:
    rows = _load(ORDERS_FILE).get("orders", [])
    if email:
        em = email.strip().lower()
        rows = [o for o in rows if (o.get("email") or "").lower() == em]
    return sorted(rows, key=lambda o: o.get("created_at", ""), reverse=True)


def get_order(order_id: str) -> dict[str, Any] | None:
    for o in _load(ORDERS_FILE).get("orders", []):
        if o.get("id") == order_id:
            return o
    return None


def create_order(
    *,
    email: str,
    items: list[dict[str, Any]],
    total_usd: float,
    payment_method: str,
    promo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _load(ORDERS_FILE)
    order_id = f"BB-{secrets.token_hex(4).upper()}"
    license_key = f"BOB-{uuid.uuid4().hex[:12].upper()}"
    order = {
        "id": order_id,
        "email": email.strip(),
        "items": items,
        "total_usd": total_usd,
        "payment_method": payment_method,
        "status": "pending",
        "promo": promo or {},
        "license_key": license_key,
        "fulfillment_token": secrets.token_urlsafe(24),
        "created_at": _now_iso(),
        "paid_at": None,
        "fulfilled_at": None,
        "refund_status": None,
    }
    data.setdefault("orders", []).append(order)
    _save_orders(data)
    return order


def mark_paid(order_id: str, *, external_id: str | None = None) -> dict[str, Any] | None:
    data = _load(ORDERS_FILE)
    for o in data.get("orders", []):
        if o.get("id") != order_id:
            continue
        o["status"] = "paid"
        o["paid_at"] = _now_iso()
        if external_id:
            o["payment_external_id"] = external_id
        _save_orders(data)
        return fulfill_order(order_id)
    return None


def fulfill_order(order_id: str) -> dict[str, Any] | None:
    data = _load(ORDERS_FILE)
    for o in data.get("orders", []):
        if o.get("id") != order_id:
            continue
        o["status"] = "fulfilled"
        o["fulfilled_at"] = _now_iso()
        o["download"] = {
            "repo": "https://github.com/YOUR_ORG/blofin-auto-trader",
            "setup_command": r'powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_god_bot.ps1',
            "ensure_command": r'powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure',
            "docs": "https://github.com/YOUR_ORG/blofin-auto-trader/blob/main/docs/GETTING_STARTED.md",
            "license_key": o.get("license_key"),
            "steps": [
                "Clone the repo (link in your order email)",
                "Copy .env.example → .env and add your Blofin API keys",
                "Run bootstrap_god_bot.ps1",
                "Start with BLOFIN_MODE=demo until you're comfortable",
                "Paste your license key in .env as BOBS_BOTS_LICENSE if prompted",
            ],
        }
        _save_orders(data)
        return o
    return None


def request_refund(
    order_id: str,
    *,
    reason: str,
    requested_by: str = "customer",
    amount_usd: float | None = None,
) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    data = _load(REFUNDS_FILE)
    refund_id = f"RF-{secrets.token_hex(4).upper()}"
    amt = amount_usd if amount_usd is not None else float(order.get("total_usd", 0))
    row = {
        "id": refund_id,
        "order_id": order_id,
        "email": order.get("email"),
        "amount_usd": round(amt, 2),
        "reason": reason[:2000],
        "status": "pending",
        "requested_by": requested_by,
        "created_at": _now_iso(),
        "resolved_at": None,
        "resolution_note": "",
    }
    data.setdefault("refunds", []).append(row)
    _save_refunds(data)

    orders = _load(ORDERS_FILE)
    for o in orders.get("orders", []):
        if o.get("id") == order_id:
            o["refund_status"] = "pending"
    _save_orders(orders)
    return row


def resolve_refund(
    refund_id: str,
    *,
    status: str,
    note: str = "",
    partial_amount: float | None = None,
) -> dict[str, Any] | None:
    data = _load(REFUNDS_FILE)
    for r in data.get("refunds", []):
        if r.get("id") != refund_id:
            continue
        r["status"] = status
        r["resolved_at"] = _now_iso()
        r["resolution_note"] = note[:2000]
        if partial_amount is not None:
            r["amount_usd"] = round(partial_amount, 2)
        _save_refunds(data)

        if status in ("approved", "partial", "store_credit"):
            orders = _load(ORDERS_FILE)
            for o in orders.get("orders", []):
                if o.get("id") == r.get("order_id"):
                    o["refund_status"] = status
            _save_orders(orders)
        return r
    return None


def list_refunds(email: str | None = None) -> list[dict[str, Any]]:
    rows = _load(REFUNDS_FILE).get("refunds", [])
    if email:
        em = email.strip().lower()
        rows = [r for r in rows if (r.get("email") or "").lower() == em]
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
