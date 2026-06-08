"""Bob's Bots — product catalog, rankings, and backtest curves."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "catalog" / "bots.json"
TA_STACK_PATH = ROOT / "catalog" / "ta_stack.json"
LEGAL_PATH = ROOT / "catalog" / "legal.json"
BRAND = "Bob's Bots"
OPERATOR = "Matthew Anthony Knight"


def load_legal() -> dict[str, Any]:
    if not LEGAL_PATH.is_file():
        return {
            "operator": OPERATOR,
            "brand": BRAND,
            "short_disclaimer": f"Operated by {OPERATOR}. Not financial advice.",
            "footer_line": f"© Bob's Bots — {OPERATOR}",
            "sections": [],
        }
    return json.loads(LEGAL_PATH.read_text(encoding="utf-8"))


def load_ta_stack() -> dict[str, Any]:
    if not TA_STACK_PATH.is_file():
        return {"summary": "", "core_ta": {}, "tier_extras": {}}
    return json.loads(TA_STACK_PATH.read_text(encoding="utf-8"))


def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.is_file():
        return {"brand": BRAND, "bots": [], "packages": [], "deals": []}
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    data["brand"] = BRAND
    return data


def get_bot(slug: str) -> dict[str, Any] | None:
    for bot in load_catalog().get("bots", []):
        if bot.get("slug") == slug or bot.get("id") == slug:
            return bot
    return None


def get_package(package_id: str) -> dict[str, Any] | None:
    for pkg in load_catalog().get("packages", []):
        if pkg.get("id") == package_id:
            return pkg
    return None


def rankings() -> list[dict[str, Any]]:
    bots = list(load_catalog().get("bots", []))
    bots.sort(key=lambda b: (b.get("backtest", {}).get("profit_scale", 0), b.get("rank", 99)), reverse=True)
    rows: list[dict[str, Any]] = []
    for i, bot in enumerate(bots):
        bt = bot.get("backtest", {})
        rows.append(
            {
                "rank": i + 1,
                "slug": bot.get("slug"),
                "name": bot.get("name"),
                "tier": bot.get("tier"),
                "total_return_pct": bt.get("total_return_pct"),
                "cagr_pct": bt.get("cagr_pct"),
                "max_drawdown_pct": bt.get("max_drawdown_pct"),
                "sharpe": bt.get("sharpe"),
                "win_rate_pct": bt.get("win_rate_pct"),
                "profit_factor": bt.get("profit_factor"),
                "risk_scale": bt.get("risk_scale"),
                "profit_scale": bt.get("profit_scale"),
                "price_usd": bot.get("price_usd"),
                "difficulty": bot.get("difficulty"),
            }
        )
    return rows


def equity_curve(bot: dict[str, Any], points: int = 120) -> list[dict[str, float]]:
    """Synthetic equity curve from backtest summary (for charts — not live data)."""
    bt = bot.get("backtest", {})
    start = float(bt.get("starting_equity", 1000))
    end = float(bt.get("ending_equity", start))
    mdd = float(bt.get("max_drawdown_pct", 15)) / 100.0
    trades = int(bt.get("trades", 1000))
    if end <= start:
        return [{"i": 0, "equity": start}, {"i": 1, "equity": end}]

    growth = end / start
    curve: list[dict[str, float]] = []
    equity = start
    peak = start
    for i in range(points):
        t = i / max(points - 1, 1)
        # Compound toward end with realistic drawdown wobble
        target = start * (growth**t)
        wobble = math.sin(i * 0.31) * mdd * peak * 0.35 + math.sin(i * 0.07) * mdd * peak * 0.15
        equity = max(start * 0.88, target + wobble)
        peak = max(peak, equity)
        curve.append({"i": float(i), "equity": round(equity, 2), "benchmark_btc": round(start * (1 + 1.86 * t), 2)})
    curve[-1]["equity"] = round(end, 2)
    return curve


def resolve_line_items(
    *,
    bot_slugs: list[str] | None = None,
    package_id: str | None = None,
    promo_code: str | None = None,
) -> tuple[list[dict[str, Any]], float, dict[str, Any]]:
    catalog = load_catalog()
    items: list[dict[str, Any]] = []
    subtotal = 0.0

    if package_id:
        pkg = get_package(package_id)
        if not pkg:
            raise ValueError(f"Unknown package: {package_id}")
        items.append(
            {
                "type": "package",
                "id": pkg["id"],
                "name": pkg["name"],
                "price_usd": pkg["price_usd"],
                "bot_ids": pkg.get("bot_ids", []),
            }
        )
        subtotal += float(pkg["price_usd"])
    elif bot_slugs:
        for slug in bot_slugs:
            bot = get_bot(slug)
            if not bot:
                raise ValueError(f"Unknown bot: {slug}")
            items.append(
                {
                    "type": "bot",
                    "id": bot["id"],
                    "slug": bot["slug"],
                    "name": bot["name"],
                    "price_usd": bot["price_usd"],
                }
            )
            subtotal += float(bot["price_usd"])
    else:
        raise ValueError("Specify bot_slugs or package_id")

    discount = 0.0
    promo_meta: dict[str, Any] = {}
    code = (promo_code or "").strip().upper()
    if code:
        for deal in catalog.get("deals", []):
            if deal.get("code", "").upper() != code:
                continue
            min_items = int(deal.get("min_items", 1))
            if len(items) < min_items:
                continue
            pct = float(deal.get("pct_off", 0))
            discount = round(subtotal * pct / 100.0, 2)
            promo_meta = {"code": code, "pct_off": pct, "label": deal.get("label", "")}
            break

    total = round(max(0.0, subtotal - discount), 2)
    return items, total, {"subtotal": subtotal, "discount": discount, "promo": promo_meta, "total": total}
