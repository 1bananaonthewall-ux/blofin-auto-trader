"""Winner-picking intelligence — session gates, correlation, regime floors, candle confirm."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings
    from conviction import RankedSetup

log = logging.getLogger(__name__)

SECTOR_MAP: dict[str, str] = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "l1",
    "AVAX": "l1",
    "SUI": "l1",
    "APT": "l1",
    "NEAR": "l1",
    "ATOM": "l1",
    "DOT": "l1",
    "ADA": "l1",
    "LINK": "infra",
    "UNI": "defi",
    "AAVE": "defi",
    "DOGE": "meme",
    "SHIB": "meme",
    "PEPE": "meme",
    "WIF": "meme",
    "BONK": "meme",
    "FET": "ai",
    "RENDER": "ai",
    "TAO": "ai",
    "WLD": "ai",
}

CORRELATED_SECTORS = frozenset({"l1", "meme", "ai", "defi"})


def symbol_sector(symbol: str) -> str:
    base = symbol.split("/")[0].split("-")[0].upper()
    return SECTOR_MAP.get(base, "alt")


def optimizer_loosen_frozen(settings: "Settings") -> bool:
    """Block gate-loosening when quality mode or live WR is weak."""
    try:
        from quality_pick import live_performance, quality_pick_active

        if quality_pick_active(settings):
            return True
        wr, _pf = live_performance(settings)
        return wr < 0.45
    except Exception:
        return bool(getattr(settings, "quality_pick_mode", True))


def book_spread_pct(ex: Any, symbol: str) -> float:
    """Live bid-ask spread from ticker feed."""
    try:
        from markets import symbol_to_inst_id

        if ex.stream:
            row = ex.stream.get_ticker(symbol)
            if row:
                bid = float(row.get("bidPrice") or row.get("bid") or 0)
                ask = float(row.get("askPrice") or row.get("ask") or 0)
                if bid > 0 and ask > 0 and ask >= bid:
                    mid = (bid + ask) / 2.0
                    return (ask - bid) / mid if mid > 0 else 0.0
        inst_id = symbol_to_inst_id(symbol)
        ticker = ex.http.get_ticker(inst_id)
        if isinstance(ticker, list) and ticker:
            ticker = ticker[0]
        if isinstance(ticker, dict):
            bid = float(ticker.get("bidPrice") or ticker.get("bid") or 0)
            ask = float(ticker.get("askPrice") or ticker.get("ask") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                return (ask - bid) / mid if mid > 0 else 0.0
    except Exception:
        pass
    return 0.0


def session_hour_blocked(
    state_dir: Path,
    *,
    min_trades: int = 15,
    cold_wr: float = 0.38,
) -> tuple[bool, str]:
    """Block entries during UTC hours with poor historical WR."""
    hour = datetime.now(timezone.utc).hour
    path = state_dir / "trade_outcomes.jsonl"
    if not path.is_file():
        return False, ""
    wins = total = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-1200:]:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            ts = int(row.get("ts") or 0)
            if ts <= 0:
                continue
            h = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).hour
            if h != hour:
                continue
            total += 1
            if row.get("outcome") == "win" or int(row.get("win", 0)) == 1:
                wins += 1
    except Exception:
        return False, ""
    if total < min_trades:
        return False, ""
    wr = wins / total
    if wr < cold_wr:
        return True, f"session hour {hour:02d}z wr={wr:.0%} n={total}"
    return False, ""


def regime_floor_adjustment(state_dir: Path, regime: str, base_floor: float) -> float:
    """Auto-tune regime pick floor from rolling live WR in that regime."""
    path = state_dir / "trade_outcomes.jsonl"
    if not path.is_file():
        return base_floor
    wins = total = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            if str(row.get("regime") or "") != regime:
                continue
            total += 1
            if row.get("outcome") == "win" or int(row.get("win", 0)) == 1:
                wins += 1
            if total >= 30:
                break
    except Exception:
        return base_floor
    if total < 8:
        return base_floor
    wr = wins / total
    if wr < 0.40:
        return min(0.72, base_floor + 0.03)
    if wr > 0.55:
        return max(0.48, base_floor - 0.02)
    return base_floor


def candle_close_confirmed(
    ohlcv_1m: list[list[float]] | None,
    side: str,
    *,
    fast_ema: float = 0.0,
) -> tuple[bool, str]:
    """Require last closed 1m candle to confirm direction."""
    if not ohlcv_1m or len(ohlcv_1m) < 3:
        return True, ""
    bar = ohlcv_1m[-2]
    if len(bar) < 5:
        return True, ""
    o, c = float(bar[1]), float(bar[4])
    side_l = str(side).lower()
    if side_l == "long":
        if c < o:
            return False, "1m close red"
        if fast_ema > 0 and c < fast_ema * 0.999:
            return False, "1m close below fast EMA"
    elif side_l == "short":
        if c > o:
            return False, "1m close green"
        if fast_ema > 0 and c > fast_ema * 1.001:
            return False, "1m close above fast EMA"
    return True, ""


def correlation_penalty(
    sym_a: str,
    sym_b: str,
    side_a: str,
    side_b: str,
) -> float:
    """Penalty when two picks are same-sector same-direction."""
    if str(side_a).lower() != str(side_b).lower():
        return 0.0
    sec_a = symbol_sector(sym_a)
    sec_b = symbol_sector(sym_b)
    if sym_a.split("/")[0] == sym_b.split("/")[0]:
        return 0.25
    if sec_a == sec_b and sec_a in CORRELATED_SECTORS:
        return 0.12
    return 0.0


def apply_correlation_ranking(ranked: list["RankedSetup"]) -> list["RankedSetup"]:
    """Demote correlated second picks so uncorrelated winners rank higher."""
    if len(ranked) < 2:
        return ranked
    adjusted: list[tuple[float, Any]] = []
    leaders: list[tuple[str, str]] = []
    for r in ranked:
        side = getattr(r.decision, "signal", None)
        side_v = side.value if hasattr(side, "value") else str(side or "")
        penalty = 0.0
        for ls, lside in leaders[:3]:
            penalty = max(penalty, correlation_penalty(ls, r.symbol, lside, side_v))
        conv = r.conviction * (1.0 - penalty)
        adjusted.append((conv, r))
        if penalty < 0.08:
            leaders.append((r.symbol, side_v))
    adjusted.sort(key=lambda x: x[0], reverse=True)
    out: list[RankedSetup] = []
    for conv, r in adjusted:
        out.append(
            type(r)(
                symbol=r.symbol,
                decision=r.decision,
                conviction=conv,
                confidence=r.confidence,
                score=r.score,
            )
        )
    return out


def select_tiered_opens(
    ranked: list["RankedSetup"],
    *,
    max_opens: int = 2,
    min_conviction: float = 0.52,
) -> list["RankedSetup"]:
    """Slot 1: apex/elite; slot 2: good+ only if slot 1 is elite+."""
    if not ranked:
        return []
    pool = [r for r in ranked if r.conviction >= min_conviction]
    if not pool:
        return []
    elite_plus = [r for r in pool if getattr(r.decision, "winner_tier", "") in ("apex", "elite")]
    good_plus = [r for r in pool if getattr(r.decision, "winner_tier", "") in ("apex", "elite", "good")]
    picks: list[RankedSetup] = []
    if elite_plus:
        picks.append(elite_plus[0])
    elif good_plus:
        picks.append(good_plus[0])
    else:
        picks.append(pool[0])
    if max_opens <= 1 or len(picks) >= max_opens:
        return picks[:max_opens]
    used = {picks[0].symbol}
    if getattr(picks[0].decision, "winner_tier", "") in ("apex", "elite"):
        for r in good_plus:
            if r.symbol not in used:
                picks.append(r)
                break
    else:
        for r in elite_plus or good_plus:
            if r.symbol not in used:
                picks.append(r)
                break
    return picks[:max_opens]
