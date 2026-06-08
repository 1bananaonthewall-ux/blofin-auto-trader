"""Bob's Bots Concierge — LLM with authority to refund, discount, and resolve disputes."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state" / "storefront"
CHAT_FILE = STATE_DIR / "concierge_chat.jsonl"
MAX_HISTORY = 16
BRAND = "Bob's Bots"

SYSTEM = f"""You are the Bob's Bots Concierge — friendly, direct, and empowered to help customers.
Bob's Bots is operated by Matthew Anthony Knight. You are NOT a financial adviser. Never tell users what to buy, sell, or how much to risk. Always remind them that trading involves substantial risk and backtests are not guarantees.

You CAN and SHOULD use your tools when appropriate:
- offer_deal: apply promo codes or custom % discounts
- create_refund: full or partial refunds, store credit
- resolve_dispute: creative resolutions (extra bot access, extended support, goodwill credit)
- grant_package_upgrade: bump customer to a higher package
- lookup_order: check order status

Rules:
- Never provide financial, investment, tax, or legal advice. Direct users to the Legal page for disclaimers.
- All bot performance claims must cite BACKTEST data only — never promise live profits.
- Be generous but fair: first refund within 7 days often approved; repeat abusers get store credit only.
- Creative dispute options: 50% refund + keep access, swap bot tier, 30-day extension, LAUNCH30 auto-apply.
- Keep replies under 400 words unless explaining install steps.
- Sign off as "— Bob's Bots Concierge" when closing a ticket.

Brand voice: confident, no hype-bro slang, honest about risk."""


def _append_chat(role: str, content: str, session_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "role": role,
        "content": content[:12000],
    }
    with CHAT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _history(session_id: str) -> list[dict[str, str]]:
    if not CHAT_FILE.is_file():
        return []
    rows: deque[dict[str, str]] = deque(maxlen=MAX_HISTORY)
    for line in CHAT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("session_id") != session_id:
            continue
        rows.append({"role": row["role"], "content": row["content"]})
    return list(rows)


def _tool_offer_deal(args: dict[str, Any]) -> dict[str, Any]:
    from storefront_catalog import load_catalog

    pct = min(50.0, max(5.0, float(args.get("pct_off", 15))))
    code = (args.get("code") or f"BOB{int(pct)}").upper()[:20]
    label = args.get("label") or f"Concierge offer — {pct:.0f}% off"
    return {
        "action": "deal_created",
        "code": code,
        "pct_off": pct,
        "label": label,
        "expires": "7 days",
        "message": f"Use code {code} at checkout for {pct:.0f}% off.",
    }


def _tool_create_refund(args: dict[str, Any]) -> dict[str, Any]:
    from storefront_orders import get_order, request_refund, resolve_refund

    order_id = str(args.get("order_id", "")).strip()
    reason = str(args.get("reason", "Concierge-initiated refund"))
    amount = args.get("amount_usd")
    auto_approve = bool(args.get("auto_approve", True))
    order = get_order(order_id)
    if not order:
        return {"action": "error", "message": f"Order {order_id} not found."}
    refund = request_refund(order_id, reason=reason, requested_by="concierge", amount_usd=amount)
    if auto_approve:
        resolve_refund(
            refund["id"],
            status="approved" if amount is None else "partial",
            note="Approved by Bob's Bots Concierge",
            partial_amount=amount,
        )
        refund["status"] = "approved"
    return {"action": "refund", "refund": refund}


def _tool_resolve_dispute(args: dict[str, Any]) -> dict[str, Any]:
    resolution = str(args.get("resolution", "store_credit"))
    note = str(args.get("note", ""))
    order_id = str(args.get("order_id", ""))
    options = {
        "full_refund": "Full refund processed — sorry for the trouble.",
        "partial_50": "50% refund + you keep bot access.",
        "store_credit": "100% store credit toward any Bob's Bots package.",
        "swap_tier": "Swapped to equivalent higher-tier bot at no charge.",
        "extend_support": "90-day concierge extension added.",
    }
    msg = options.get(resolution, note or "Custom resolution applied.")
    result: dict[str, Any] = {"action": "dispute_resolved", "resolution": resolution, "message": msg, "order_id": order_id}
    if resolution in ("full_refund", "partial_50", "store_credit") and order_id:
        from storefront_orders import request_refund, resolve_refund, get_order

        order = get_order(order_id)
        if order:
            amt = float(order.get("total_usd", 0))
            if resolution == "partial_50":
                amt *= 0.5
            elif resolution == "store_credit":
                result["store_credit_usd"] = amt
                return result
            refund = request_refund(order_id, reason=note or resolution, requested_by="concierge", amount_usd=amt)
            resolve_refund(refund["id"], status="approved" if resolution == "full_refund" else "partial", note=msg, partial_amount=amt)
            result["refund_id"] = refund["id"]
    return result


def _tool_lookup_order(args: dict[str, Any]) -> dict[str, Any]:
    from storefront_orders import get_order, list_orders

    order_id = str(args.get("order_id", "")).strip()
    email = str(args.get("email", "")).strip()
    if order_id:
        o = get_order(order_id)
        return {"action": "order", "order": o} if o else {"action": "error", "message": "Not found"}
    if email:
        return {"action": "orders", "orders": list_orders(email)[:5]}
    return {"action": "error", "message": "Provide order_id or email"}


def _tool_grant_upgrade(args: dict[str, Any]) -> dict[str, Any]:
    package = str(args.get("package_id", "pro"))
    return {
        "action": "upgrade_granted",
        "package_id": package,
        "message": f"Upgrade to {package} package applied — check your email for new license.",
    }


TOOLS: dict[str, Any] = {
    "offer_deal": _tool_offer_deal,
    "create_refund": _tool_create_refund,
    "resolve_dispute": _tool_resolve_dispute,
    "lookup_order": _tool_lookup_order,
    "grant_package_upgrade": _tool_grant_upgrade,
}


def _parse_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for m in re.finditer(r"```tool\s*(\w+)\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            calls.append((m.group(1), json.loads(m.group(2))))
        except Exception:
            pass
    return calls


def _keyword_fallback(message: str, session_id: str) -> tuple[str, list[dict[str, Any]]]:
    low = message.lower()
    actions: list[dict[str, Any]] = []
    if "refund" in low:
        actions.append(_tool_offer_deal({"pct_off": 15, "code": "SORRY15", "label": "Goodwill discount"}))
        reply = (
            "I can process refunds within 7 days of purchase — no questions asked for demo-mode issues. "
            "Share your order ID (starts with BB-) and I'll look it up. "
            "Alternatively, try code SORRY15 for 15% off your next bot.\n\n— Bob's Bots Concierge"
        )
        return reply, actions
    if "install" in low or "setup" in low:
        return (
            "Install in 3 steps: (1) clone repo, (2) run `scripts\\bootstrap_god_bot.ps1`, "
            "(3) add Blofin keys to `.env` and run `God Bot.ps1 -Action ensure`. "
            "Always start in demo mode. Full guide ships with your order.\n\n— Bob's Bots Concierge",
            actions,
        )
    if "backtest" in low or "proof" in low or "performance" in low:
        return (
            "Every Bob's Bot ships with published backtest metrics — CAGR, Sharpe, max drawdown, and benchmark vs BTC hold. "
            "We sell on simulated history, not live PnL screenshots. Rankings on the homepage sort bots by backtest profit scale.\n\n— Bob's Bots Concierge",
            actions,
        )
    if "deal" in low or "discount" in low or "coupon" in low:
        actions.append(_tool_offer_deal({"pct_off": 30, "code": "LAUNCH30"}))
        return (
            "Launch week code **LAUNCH30** takes 30% off any bot. "
            "Buying 2+ bots? **STACK20** stacks another 20%. Ask me for a custom bundle price.\n\n— Bob's Bots Concierge",
            actions,
        )
    return (
        "Hey — I'm the Bob's Bots Concierge. I can help with installs, backtest questions, refunds, disputes, and custom deals. "
        "What are you looking for?\n\n— Bob's Bots Concierge",
        actions,
    )


def concierge_reply(message: str, *, session_id: str = "default", email: str | None = None) -> dict[str, Any]:
    message = (message or "").strip()
    if not message:
        return {"reply": "Ask me anything about Bob's Bots.", "actions": []}

    _append_chat("user", message, session_id)
    actions: list[dict[str, Any]] = []
    reply = ""

    try:
        from local_llm import chat_completion, resolve_provider

        if resolve_provider() != "none":
            catalog_snip = ""
            try:
                from storefront_catalog import load_catalog, rankings

                top = rankings()[:3]
                catalog_snip = "Top backtest ranks: " + ", ".join(f"{r['name']} ({r['total_return_pct']}%)" for r in top)
            except Exception:
                pass

            ctx = f"Customer email hint: {email or 'unknown'}. {catalog_snip}"
            messages = [{"role": "system", "content": SYSTEM + "\n" + ctx}]
            messages.extend(_history(session_id))
            messages.append({"role": "user", "content": message})
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "To invoke a tool, include a fenced block: ```tool tool_name {\"arg\": \"value\"}``` "
                        "Valid tools: offer_deal, create_refund, resolve_dispute, lookup_order, grant_package_upgrade"
                    ),
                }
            )
            raw = chat_completion(messages, max_tokens=int(os.environ.get("STOREFRONT_LLM_MAX_TOKENS", "600")), temperature=0.35)
            reply = (raw or "").strip()
            for name, args in _parse_tool_calls(reply):
                fn = TOOLS.get(name)
                if fn:
                    try:
                        actions.append(fn(args))
                    except Exception as exc:
                        actions.append({"action": "error", "tool": name, "message": str(exc)})
            reply = re.sub(r"```tool.*?```", "", reply, flags=re.DOTALL).strip()
    except Exception as exc:
        log.debug("LLM concierge fallback: %s", exc)

    if not reply:
        reply, kw_actions = _keyword_fallback(message, session_id)
        actions.extend(kw_actions)

    _append_chat("assistant", reply, session_id)
    return {"reply": reply, "actions": actions, "session_id": session_id}
