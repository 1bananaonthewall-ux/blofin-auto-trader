"""Bob's Bots — Stripe cards + crypto checkout."""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any

import requests

STRIPE_SECRET = os.environ.get("STOREFRONT_STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK = os.environ.get("STOREFRONT_STRIPE_WEBHOOK_SECRET", "").strip()
SITE_URL = os.environ.get("STOREFRONT_SITE_URL", "http://127.0.0.1:5070").rstrip("/")

CRYPTO_BTC = os.environ.get("STOREFRONT_CRYPTO_BTC_ADDRESS", "").strip()
CRYPTO_ETH = os.environ.get("STOREFRONT_CRYPTO_ETH_ADDRESS", "").strip()
CRYPTO_USDT = os.environ.get("STOREFRONT_CRYPTO_USDT_TRC20", "").strip()


def payments_configured() -> dict[str, bool]:
    return {
        "stripe": bool(STRIPE_SECRET),
        "crypto_btc": bool(CRYPTO_BTC),
        "crypto_eth": bool(CRYPTO_ETH),
        "crypto_usdt": bool(CRYPTO_USDT),
        "demo_mode": not bool(STRIPE_SECRET),
    }


def create_stripe_checkout(
    *,
    order_id: str,
    email: str,
    total_usd: float,
    line_description: str,
) -> dict[str, Any]:
    if not STRIPE_SECRET:
        return {
            "mode": "demo",
            "checkout_url": f"{SITE_URL}/?order={order_id}&demo_paid=1",
            "message": "Stripe not configured — use demo checkout or crypto.",
        }

    cents = int(round(total_usd * 100))
    payload = {
        "mode": "payment",
        "customer_email": email,
        "success_url": f"{SITE_URL}/?order={order_id}&paid=1",
        "cancel_url": f"{SITE_URL}/?checkout=cancel",
        "metadata[order_id]": order_id,
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": cents,
        "line_items[0][price_data][product_data][name]": f"Bob's Bots — {line_description}",
        "line_items[0][quantity]": 1,
    }
    resp = requests.post(
        "https://api.stripe.com/v1/checkout/sessions",
        data=payload,
        auth=(STRIPE_SECRET, ""),
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return {"mode": "stripe", "checkout_url": body["url"], "session_id": body["id"]}


def _unique_crypto_amount(total_usd: float, order_id: str) -> float:
    """Add tiny suffix so we can match incoming payments."""
    h = int(hashlib.sha256(order_id.encode()).hexdigest()[:4], 16)
    suffix = (h % 97) / 100_000.0
    return round(total_usd + suffix, 5)


def create_crypto_invoice(*, order_id: str, total_usd: float, asset: str) -> dict[str, Any]:
    asset = asset.upper()
    amount = _unique_crypto_amount(total_usd, order_id)
    addresses = {
        "BTC": CRYPTO_BTC or "bc1q-demo-bobs-bots-set-STOREFRONT_CRYPTO_BTC_ADDRESS",
        "ETH": CRYPTO_ETH or "0xDemoBobsBotsSetSTOREFRONT_CRYPTO_ETH_ADDRESS",
        "USDT": CRYPTO_USDT or "TDemoBobsBotsSetSTOREFRONT_CRYPTO_USDT_TRC20",
    }
    addr = addresses.get(asset, addresses["USDT"])
    return {
        "order_id": order_id,
        "asset": asset,
        "amount": amount,
        "amount_usd": total_usd,
        "address": addr,
        "expires_minutes": 60,
        "instructions": (
            f"Send exactly {amount} {asset} to the address below. "
            "Include order ID in memo if your wallet supports it. "
            "Bob's concierge will confirm within 15 minutes or auto-match when configured."
        ),
        "demo_mode": not any([CRYPTO_BTC, CRYPTO_ETH, CRYPTO_USDT]),
    }


def verify_stripe_webhook(payload: bytes, sig_header: str) -> dict[str, Any] | None:
    if not STRIPE_WEBHOOK:
        return None
    try:
        import stripe  # type: ignore

        return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK)
    except Exception:
        return None
