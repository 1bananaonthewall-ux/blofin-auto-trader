"""
Exchange TP/SL validation and price extraction.

Blofin may return multiple pending TPSL rows; long/short use opposite
extrema for SL vs TP. Never treat "any pending row" as healthy without
matching trigger prices to the computed targets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Max relative drift between intended and live trigger before re-placing.
DEFAULT_TOL_PCT = 0.0025
# Wider tolerance — avoid cancel/replace when both legs exist and side is correct.
ADEQUATE_TOL_PCT = 0.012
# Min gap between trigger and last/mark (Blofin 102040).
MARKET_GAP_PCT = 0.002


@dataclass(frozen=True)
class PendingTpsl:
    sl_price: float
    tp_price: float
    live_rows: int
    has_sl: bool
    has_tp: bool
    issues: tuple[str, ...]


def _live_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        state = str(row.get("state") or "live").strip().lower()
        if state in ("", "live", "effective"):
            out.append(row)
    return out


def _parse_trigger(row: dict, *keys: str) -> float:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "" or str(raw).lower() == "null":
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def _row_position_side(row: dict) -> str:
    return str(row.get("positionSide") or row.get("posSide") or "").lower()


def filter_pending_rows(
    rows: list[dict], *, position_side: str | None = None
) -> list[dict]:
    """Keep live TPSL rows; in hedge mode prefer matching positionSide."""
    live = _live_rows(rows)
    if not position_side:
        return live
    ps = position_side.lower()
    if ps in ("", "net"):
        return live
    matched = [r for r in live if _row_position_side(r) in ("", "net", ps)]
    return matched if matched else live


def pending_from_registry_prices(
    side: str, entry: float, sl_price: float, tp_price: float
) -> PendingTpsl | None:
    """Synthetic pending view from persisted SL/TP (when REST pending fetch is empty)."""
    if sl_price <= 0 or tp_price <= 0 or entry <= 0:
        return None
    side_l = side.lower()
    if side_l == "long":
        sl = sl_price
        tp = tp_price
    else:
        sl = sl_price
        tp = tp_price
    issues: list[str] = []
    if side_l == "long":
        if sl >= entry:
            issues.append("sl_wrong_side")
        if tp <= entry:
            issues.append("tp_wrong_side")
    else:
        if sl <= entry:
            issues.append("sl_wrong_side")
        if tp >= entry:
            issues.append("tp_wrong_side")
    return PendingTpsl(
        sl_price=sl,
        tp_price=tp,
        live_rows=0,
        has_sl=True,
        has_tp=True,
        issues=tuple((*issues, "registry_only")),
    )


def extract_pending_tpsl(
    side: str,
    entry: float,
    rows: list[dict],
    *,
    position_side: str | None = None,
) -> PendingTpsl:
    """Aggregate SL/TP trigger prices from pending TPSL orders."""
    live = filter_pending_rows(rows, position_side=position_side)
    sl_prices = [
        _parse_trigger(r, "slTriggerPrice", "slTriggerPx", "stopTriggerPrice")
        for r in live
    ]
    sl_prices = [p for p in sl_prices if p > 0]
    tp_prices = [
        _parse_trigger(r, "tpTriggerPrice", "tpTriggerPx", "takeProfitTriggerPrice")
        for r in live
    ]
    tp_prices = [p for p in tp_prices if p > 0]
    issues: list[str] = []
    if not sl_prices:
        issues.append("missing_sl")
    if not tp_prices:
        issues.append("missing_tp")
    if not live:
        issues.append("no_live_tpsl")

    side_l = side.lower()
    if side_l == "long":
        sl = min(sl_prices) if sl_prices else 0.0
        tp = max(tp_prices) if tp_prices else 0.0
        if sl_prices and entry > 0 and sl >= entry:
            issues.append("sl_wrong_side")
        if tp_prices and entry > 0 and tp <= entry:
            issues.append("tp_wrong_side")
    else:
        sl = max(sl_prices) if sl_prices else 0.0
        tp = min(tp_prices) if tp_prices else 0.0
        if sl_prices and entry > 0 and sl <= entry:
            issues.append("sl_wrong_side")
        if tp_prices and entry > 0 and tp >= entry:
            issues.append("tp_wrong_side")

    return PendingTpsl(
        sl_price=sl,
        tp_price=tp,
        live_rows=len(live),
        has_sl=bool(sl_prices),
        has_tp=bool(tp_prices),
        issues=tuple(issues),
    )


def price_near(a: float, b: float, *, tol_pct: float = DEFAULT_TOL_PCT) -> bool:
    if a <= 0 or b <= 0:
        return False
    mid = (a + b) / 2.0
    return abs(a - b) / max(mid, 1e-12) <= tol_pct


def pending_matches_targets(
    side: str,
    entry: float,
    pending: PendingTpsl,
    target_sl: float,
    target_tp: float,
    *,
    tol_pct: float = DEFAULT_TOL_PCT,
) -> tuple[bool, tuple[str, ...]]:
    """True when exchange pending SL+TP match our computed triggers."""
    issues = list(pending.issues)
    if not pending.has_sl or not pending.has_tp:
        return False, tuple(issues)
    if not price_near(pending.sl_price, target_sl, tol_pct=tol_pct):
        issues.append(
            f"sl_drift live={pending.sl_price:.6f} want={target_sl:.6f}"
        )
    if not price_near(pending.tp_price, target_tp, tol_pct=tol_pct):
        issues.append(
            f"tp_drift live={pending.tp_price:.6f} want={target_tp:.6f}"
        )
    return (not issues, tuple(issues))


def pending_exceeds_policy_caps(
    side: str,
    entry: float,
    pending: PendingTpsl,
    max_stop_pct: float,
    max_take_pct: float,
    *,
    slack: float = 1.12,
) -> bool:
    """True when exchange brackets are wider than policy (stale liq-gap TPSL)."""
    if entry <= 0 or not pending.has_sl or not pending.has_tp:
        return False
    sp = abs(entry - pending.sl_price) / entry if pending.sl_price > 0 else 0.0
    tp = abs(pending.tp_price - entry) / entry if pending.tp_price > 0 else 0.0
    if max_stop_pct > 0 and sp > max_stop_pct * slack:
        return True
    if max_take_pct > 0 and tp > max_take_pct * slack:
        return True
    return False


def pending_is_adequate(side: str, entry: float, pending: PendingTpsl) -> bool:
    """Both SL+TP live on exchange (REST pending) on the correct side of entry."""
    if pending.live_rows <= 0:
        return False
    if not pending.has_sl or not pending.has_tp:
        return False
    bad = {"sl_wrong_side", "tp_wrong_side", "missing_sl", "missing_tp", "no_live_tpsl"}
    if bad.intersection(set(pending.issues)):
        return False
    if entry <= 0:
        return True
    side_l = side.lower()
    if side_l == "long":
        return pending.sl_price < entry < pending.tp_price
    return pending.tp_price < entry < pending.sl_price


def adjust_triggers_for_market(
    side: str,
    sl_price: float,
    tp_price: float,
    mark: float,
    *,
    gap_pct: float = MARKET_GAP_PCT,
) -> tuple[float, float]:
    """
    Blofin requires short SL triggers above last price and long SL below last.
    Nudge triggers when price has moved through the intended level (error 102040).
    """
    if mark <= 0:
        # Never zero triggers when exchange mark is unavailable (caller will retry mark).
        return sl_price, tp_price
    gap = max(gap_pct, 0.0005)
    side_l = side.lower()
    if side_l == "short":
        min_sl = mark * (1 + gap)
        max_tp = mark * (1 - gap)
        if sl_price > 0:
            sl_price = max(sl_price, min_sl)
        if tp_price > 0:
            tp_price = min(tp_price, max_tp)
    else:
        max_sl = mark * (1 - gap)
        min_tp = mark * (1 + gap)
        if sl_price > 0:
            sl_price = min(sl_price, max_sl)
        if tp_price > 0:
            tp_price = max(tp_price, min_tp)
    return sl_price, tp_price


def pct_from_prices(side: str, entry: float, sl: float, tp: float) -> tuple[float, float]:
    if entry <= 0:
        return 0.0, 0.0
    side_l = side.lower()
    if side_l == "long":
        stop_pct = max(0.0, (entry - sl) / entry)
        take_pct = max(0.0, (tp - entry) / entry)
    else:
        stop_pct = max(0.0, (sl - entry) / entry)
        take_pct = max(0.0, (entry - tp) / entry)
    return stop_pct, take_pct
