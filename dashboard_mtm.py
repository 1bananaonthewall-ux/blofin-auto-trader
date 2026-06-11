"""Mark-to-market equity — tick-level balance from live marks + REST anchor."""

from __future__ import annotations

from typing import Any

from exchange_client import BlofinExchange
from market_stream import BlofinMarketStream


def mtm_positions(
    positions: list[dict[str, Any]],
    stream: BlofinMarketStream | None,
) -> tuple[list[dict[str, Any]], float]:
    """Refresh marks/ROE from WS tickers; return rows + total unrealized USD."""
    if not positions:
        return [], 0.0
    out: list[dict[str, Any]] = []
    total_unreal = 0.0
    for p in positions:
        sym = str(p.get("symbol") or "")
        entry = float(p.get("entry") or 0)
        side = str(p.get("side") or "long").lower()
        margin = float(p.get("margin_usdt") or 0)
        lev = int(p.get("leverage") or 0)
        contracts = float(p.get("contracts") or 0)
        mark = float(p.get("mark") or entry)
        if stream and sym:
            live = stream.get_last_price(sym)
            if live and live > 0:
                mark = float(live)
        roe, pnl, notional, eff = BlofinExchange.position_display_metrics(
            side=side,
            entry=entry,
            mark=mark,
            margin_usdt=margin,
            leverage=lev,
            contracts=contracts,
        )
        row = dict(p)
        row["mark"] = round(mark, 8) if mark else mark
        row["pnl_pct"] = roe
        row["pnl_usd"] = pnl
        row["notional_usdt"] = notional
        row["effective_leverage"] = round(eff, 1) if eff else lev
        if stream and sym and stream.get_last_price(sym):
            row["mtm"] = True
        out.append(row)
        total_unreal += float(pnl)
    return out, round(total_unreal, 6)


def mtm_equity(
    *,
    anchor_equity: float,
    anchor_unrealized: float,
    current_unrealized: float,
) -> float:
    """Wallet equity at REST anchor + live unrealized delta."""
    if anchor_equity <= 0:
        return 0.0
    return round(anchor_equity - anchor_unrealized + current_unrealized, 6)


def mtm_free_margin(
    *,
    anchor_free: float,
    anchor_unrealized: float,
    current_unrealized: float,
) -> float:
    if anchor_free <= 0:
        return 0.0
    return round(max(0.0, anchor_free - (current_unrealized - anchor_unrealized)), 6)
