"""
Instant universe ratings from WS/REST tickers + symbol quality (no per-symbol LLM).

Refreshed every bot tick; feeds scan priority and LLM overseer summaries.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from markets import symbol_to_inst_id

if TYPE_CHECKING:
    from exchange_client import BlofinExchange
    from symbol_quality import SymbolQualityStore

log = logging.getLogger(__name__)

RATINGS_FILE = "universe_ratings.json"


def _tier(composite: float) -> str:
    if composite >= 0.72:
        return "A"
    if composite >= 0.55:
        return "B"
    if composite >= 0.38:
        return "C"
    return "D"


def refresh_ratings(
    ex: "BlofinExchange",
    symbols: list[str],
    quality_store: "SymbolQualityStore | None",
    state_dir: Path,
) -> dict[str, Any]:
    stream = ex.stream
    rows: list[dict[str, Any]] = []
    now = time.time()

    for sym in symbols:
        chg24 = 0.0
        vol = 0.0
        last = 0.0
        if stream is not None:
            row = stream.get_ticker(sym)
            if row:
                try:
                    last = float(row.get("last") or row.get("lastPrice") or 0)
                    open24 = float(row.get("open24h") or last)
                    vol = float(row.get("vol24h") or row.get("volCurrency24h") or 0)
                    if open24 > 0 and last > 0:
                        chg24 = (last - open24) / open24
                except (TypeError, ValueError):
                    pass
        momentum = abs(chg24) * math.log1p(max(vol, 0.0))
        quality = quality_store.score(sym) if quality_store else 0.5
        composite = min(1.0, momentum * 2.8 + quality * 0.45 + min(abs(chg24) * 12.0, 0.25))
        rows.append(
            {
                "symbol": sym,
                "last": last,
                "chg24_pct": round(chg24 * 100.0, 3),
                "vol24": vol,
                "momentum": round(momentum, 5),
                "quality": round(quality, 3),
                "composite": round(composite, 4),
                "tier": _tier(composite),
            }
        )

    rows.sort(key=lambda r: r["composite"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    payload = {
        "updated_ts": now,
        "count": len(rows),
        "top": rows[:40],
        "all": rows,
    }
    path = state_dir / RATINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return payload


def load_ratings(state_dir: Path) -> dict[str, Any]:
    path = state_dir / RATINGS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def prioritize_scan(scan: list[str], state_dir: Path, prefer: list[str] | None = None) -> list[str]:
    """Reorder scan list: overseer prefer + top-rated first, then rotation tail."""
    ratings = load_ratings(state_dir)
    rank_map = {r["symbol"]: r["rank"] for r in ratings.get("all") or []}
    prefer_set = {s for s in (prefer or []) if s}

    def key(sym: str) -> tuple[int, int, str]:
        pref = 0 if sym in prefer_set else 1
        rk = rank_map.get(sym, 9999)
        return (pref, rk, sym)

    ordered = sorted(scan, key=key)
    return ordered


def top_symbols(state_dir: Path, n: int = 20) -> list[str]:
    data = load_ratings(state_dir)
    return [r["symbol"] for r in (data.get("top") or [])[:n]]
