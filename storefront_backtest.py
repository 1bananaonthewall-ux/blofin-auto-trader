"""Bob's Bots backtests — real confluence engine via bobs_bots package."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from bobs_bots.period import resolve_backtest_range
from bobs_bots.specs import get_spec

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "state" / "storefront" / "backtest_cache"

# Legacy slug map for catalog products
_CATALOG_ALIASES = {
    "god-bot-conservative": "god-bot-scalper-pro",
    "universe-scanner": "god-bot-3r-fast",
}


def _cache_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_cache(key: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("cached_at", 0)) > 7200:
            return None
        return data
    except Exception:
        return None


def _save_cache(key: str, data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["cached_at"] = time.time()
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def _resolve_bot_slug(slug: str) -> str:
    return _CATALOG_ALIASES.get(slug, slug)


def run_backtest(
    *,
    bot_slug: str,
    starting_pot: float,
    bar: str = "5m",
    inst_ids: list[str] | None = None,
    max_assets: int = 60,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    del bar  # engine uses 5m + 1H (legit confluence windows)
    starting_pot = max(10.0, min(1_000_000.0, float(starting_pot)))
    period = resolve_backtest_range(start_date=start_date, end_date=end_date, lookback_days=lookback_days)
    spec = get_spec(_resolve_bot_slug(bot_slug))

    payload = {
        "bot_slug": spec.id,
        "starting_pot": starting_pot,
        "start_date": period["start_date"],
        "end_date": period["end_date"],
        "inst_ids": inst_ids or [],
        "max_assets": max_assets,
        "engine": "bobs_bots_confluence_v1",
    }
    key = _cache_key(payload)
    cached = _load_cache(key)
    if cached:
        cached["from_cache"] = True
        return cached

    from bobs_bots.simulator import backtest_symbol
    from storefront_market import list_tradeable_assets

    all_assets = list_tradeable_assets()
    assets = all_assets
    if inst_ids:
        allow = set(inst_ids)
        assets = [a for a in all_assets if a["inst_id"] in allow]
    else:
        assets = assets[:max_assets]

    results: list[dict[str, Any]] = []
    errors = 0
    for asset in assets:
        try:
            row = backtest_symbol(
                spec,
                inst_id=asset["inst_id"],
                starting_pot=starting_pot,
                start_ms=period["start_ms"],
                end_ms=period["end_ms"],
                asset_meta=asset,
            )
            if row.get("error"):
                errors += 1
                continue
            row["tradingview_url"] = f"https://www.tradingview.com/chart/?symbol={asset['tradingview']}"
            results.append(row)
        except Exception as exc:
            log.debug("backtest %s failed: %s", asset["inst_id"], exc)
            errors += 1
        time.sleep(0.04)

    results.sort(key=lambda r: r.get("return_pct", 0), reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    out = {
        "brand": "Bob's Bots",
        "bot_slug": bot_slug,
        "bot_id": spec.id,
        "bot_name": spec.name,
        "engine": "confluence_5m_1h",
        "ta_note": spec.description,
        "profile": {
            "min_confluence": spec.min_confluence,
            "min_agreeing": spec.min_agreeing,
            "min_score": spec.min_composite_score,
            "three_r": spec.three_r_mode,
            "runner_filter": spec.runner_filter,
            "require_runner": spec.require_runner,
        },
        "starting_pot": starting_pot,
        "lookback_days": period["lookback_days"],
        "start_date": period["start_date"],
        "end_date": period["end_date"],
        "period_start": period["start_date"],
        "period_end": period["end_date"],
        "assets_tested": len(results),
        "assets_errors": errors,
        "total_universe": len(all_assets),
        "results": results,
        "disclaimer": (
            "Simulated backtest on Blofin 5m/1H OHLCV — not financial advice. "
            "Operated by Matthew Anthony Knight. Past results do not guarantee future returns. "
            "Trading leveraged crypto involves substantial risk of loss."
        ),
        "from_cache": False,
    }
    _save_cache(key, out)
    return out


def pine_script_for_bot(bot_slug: str, starting_pot: float) -> str:
    spec = get_spec(_resolve_bot_slug(bot_slug))
    stop = spec.max_stop_pct * 100
    take = spec.max_take_pct * 100
    return f"""// Bob's Bots — {spec.name} (confluence gates: cs>={spec.min_confluence}, votes>={spec.min_agreeing})
//@version=5
strategy("{spec.name}", overlay=true, initial_capital={int(starting_pot)}, default_qty_type=strategy.percent_of_equity, default_qty_value={spec.risk_per_trade * 100})
stopPct = {stop:.3f} / 100
takePct = {take:.3f} / 100
ema9 = ta.ema(close, 9)
ema21 = ta.ema(close, 21)
rsi14 = ta.rsi(close, 14)
[_, _, macdHist] = ta.macd(close, 12, 26, 9)
longSig = ema9 > ema21 and rsi14 < 72 and macdHist > 0
shortSig = ema9 < ema21 and rsi14 > 28 and macdHist < 0
if longSig
    strategy.entry("L", strategy.long)
if shortSig
    strategy.entry("S", strategy.short)
strategy.exit("XL", "L", stop=close*(1-stopPct), limit=close*(1+takePct))
strategy.exit("XS", "S", stop=close*(1+stopPct), limit=close*(1-takePct))
"""
