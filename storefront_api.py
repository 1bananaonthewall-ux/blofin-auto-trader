#!/usr/bin/env python3
"""Bob's Bots — public storefront API (catalog, checkout, concierge). Port 5070."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)
app = Flask(__name__, static_folder="storefront/dist", static_url_path="")
PORT = int(os.environ.get("STOREFRONT_PORT", "5070"))


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    allowed = os.environ.get("STOREFRONT_CORS_ORIGIN", "*")
    if allowed == "*" or origin:
        resp.headers["Access-Control-Allow-Origin"] = allowed if allowed != "*" else (origin or "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/health")
def health():
    from storefront_payments import payments_configured

    from storefront_catalog import OPERATOR, load_legal

    return jsonify(
        {
            "ok": True,
            "brand": "Bob's Bots",
            "operator": OPERATOR,
            "legal": load_legal().get("short_disclaimer", ""),
            "payments": payments_configured(),
        }
    )


@app.route("/api/catalog")
def api_catalog():
    from storefront_catalog import load_catalog, load_legal, load_ta_stack, rankings

    cat = load_catalog()
    return jsonify({**cat, "rankings": rankings(), "ta_stack": load_ta_stack(), "legal": load_legal()})


@app.route("/api/legal")
def api_legal():
    from storefront_catalog import load_legal

    return jsonify(load_legal())


@app.route("/api/ta-stack")
def api_ta_stack():
    from storefront_catalog import load_ta_stack

    return jsonify(load_ta_stack())


@app.route("/api/bots/<slug>")
def api_bot(slug: str):
    from storefront_catalog import equity_curve, get_bot

    bot = get_bot(slug)
    if not bot:
        return jsonify({"error": "not found"}), 404
    return jsonify({"bot": bot, "equity_curve": equity_curve(bot)})


@app.route("/api/quote", methods=["POST"])
def api_quote():
    from storefront_catalog import resolve_line_items

    body = request.get_json(silent=True) or {}
    try:
        items, total, meta = resolve_line_items(
            bot_slugs=body.get("bot_slugs"),
            package_id=body.get("package_id"),
            promo_code=body.get("promo_code"),
        )
        return jsonify({"items": items, **meta})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/checkout", methods=["POST"])
def api_checkout():
    from storefront_catalog import resolve_line_items
    from storefront_orders import create_order, mark_paid
    from storefront_payments import create_crypto_invoice, create_stripe_checkout

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400

    method = (body.get("payment_method") or "card").lower()
    try:
        items, total, meta = resolve_line_items(
            bot_slugs=body.get("bot_slugs"),
            package_id=body.get("package_id"),
            promo_code=body.get("promo_code"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    order = create_order(
        email=email,
        items=items,
        total_usd=total,
        payment_method=method,
        promo=meta.get("promo"),
    )
    desc = items[0]["name"] if len(items) == 1 else f"{len(items)} items"

    if method == "card":
        pay = create_stripe_checkout(order_id=order["id"], email=email, total_usd=total, line_description=desc)
        if pay.get("mode") == "demo" and body.get("demo_confirm"):
            mark_paid(order["id"])
            order = mark_paid(order["id"]) or order
        return jsonify({"order": order, "payment": pay})

    asset = (body.get("crypto_asset") or "USDT").upper()
    invoice = create_crypto_invoice(order_id=order["id"], total_usd=total, asset=asset)
    if body.get("demo_confirm"):
        fulfilled = mark_paid(order["id"])
        return jsonify({"order": fulfilled or order, "payment": invoice, "demo_fulfilled": True})
    return jsonify({"order": order, "payment": invoice})


@app.route("/api/orders/<order_id>")
def api_order(order_id: str):
    from storefront_orders import get_order

    token = request.args.get("token", "")
    order = get_order(order_id)
    if not order:
        return jsonify({"error": "not found"}), 404
    if token and token != order.get("fulfillment_token"):
        return jsonify({"error": "invalid token"}), 403
    safe = {k: v for k, v in order.items() if k != "fulfillment_token"}
    return jsonify({"order": safe})


@app.route("/api/concierge", methods=["POST"])
def api_concierge():
    from storefront_concierge import concierge_reply

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    session_id = (body.get("session_id") or "web").strip()[:64]
    email = (body.get("email") or "").strip() or None
    return jsonify(concierge_reply(message, session_id=session_id, email=email))


@app.route("/api/backtest/assets")
def api_backtest_assets():
    from storefront_market import list_tradeable_assets

    rows = list_tradeable_assets()
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    return jsonify(
        {
            "count": len(rows),
            "assets": rows,
            "max_lookback_days": 730,
            "max_start_date": (today - timedelta(days=730)).isoformat(),
            "max_end_date": today.isoformat(),
        }
    )


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    from storefront_backtest import run_backtest

    body = request.get_json(silent=True) or {}
    try:
        result = run_backtest(
            bot_slug=(body.get("bot_slug") or "god-bot-scalper-pro").strip(),
            starting_pot=float(body.get("starting_pot", 1000)),
            start_date=(body.get("start_date") or "").strip() or None,
            end_date=(body.get("end_date") or "").strip() or None,
            lookback_days=body.get("lookback_days"),
            bar=(body.get("bar") or "4H").strip(),
            inst_ids=body.get("inst_ids"),
            max_assets=int(body.get("max_assets", 60)),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("backtest run failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/backtest/symbol/<inst_id>")
def api_backtest_symbol(inst_id: str):
    from bobs_bots.period import resolve_backtest_range
    from bobs_bots.simulator import backtest_symbol
    from bobs_bots.specs import get_spec
    from storefront_market import list_tradeable_assets

    bot_slug = request.args.get("bot_slug", "god-bot-scalper-pro")
    starting_pot = float(request.args.get("starting_pot", 1000))
    bar = request.args.get("bar", "4H")
    starting_pot = max(10.0, min(1_000_000.0, starting_pot))
    try:
        period = resolve_backtest_range(
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            lookback_days=request.args.get("lookback_days", type=int),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    spec = get_spec(bot_slug)
    meta = next((a for a in list_tradeable_assets() if a["inst_id"] == inst_id), None)
    if not meta:
        return jsonify({"error": "unknown asset"}), 404
    sim = backtest_symbol(
        spec,
        inst_id=inst_id,
        starting_pot=starting_pot,
        start_ms=period["start_ms"],
        end_ms=period["end_ms"],
        asset_meta=meta,
    )
    if sim.get("error"):
        return jsonify(sim), 400
    return jsonify(
        {
            **sim,
            "tradingview_url": f"https://www.tradingview.com/chart/?symbol={meta['tradingview']}",
            "bot_slug": bot_slug,
            "lookback_days": period["lookback_days"],
            "start_date": period["start_date"],
            "end_date": period["end_date"],
            "bar": "5m",
        }
    )


@app.route("/api/backtest/pine")
def api_backtest_pine():
    from storefront_backtest import pine_script_for_bot

    bot_slug = request.args.get("bot_slug", "god-bot-scalper-pro")
    starting_pot = float(request.args.get("starting_pot", 1000))
    return jsonify(
        {
            "bot_slug": bot_slug,
            "starting_pot": starting_pot,
            "pine": pine_script_for_bot(bot_slug, starting_pot),
            "instructions": "Paste into TradingView Pine Editor, add to chart, open Strategy Tester, set date range up to 2 years.",
        }
    )


@app.route("/api/refunds", methods=["POST"])
def api_refunds():
    from storefront_orders import request_refund

    body = request.get_json(silent=True) or {}
    order_id = (body.get("order_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not order_id or not reason:
        return jsonify({"error": "order_id and reason required"}), 400
    try:
        refund = request_refund(order_id, reason=reason)
        return jsonify({"refund": refund, "message": "Refund request received — concierge will respond within 24h."})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/")
def index():
    dist = ROOT / "storefront" / "dist" / "index.html"
    if dist.is_file():
        return send_from_directory(str(dist.parent), "index.html")
    return (
        "<h1>Bob's Bots</h1><p>Build the storefront: <code>cd storefront && npm run build</code></p>",
        200,
        {"Content-Type": "text/html"},
    )


@app.route("/<path:path>")
def static_proxy(path: str):
    dist_dir = ROOT / "storefront" / "dist"
    target = dist_dir / path
    if target.is_file():
        return send_from_directory(str(dist_dir), path)
    index = dist_dir / "index.html"
    if index.is_file():
        return send_from_directory(str(dist_dir), "index.html")
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Bob's Bots storefront -> http://127.0.0.1:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
