"""
Qwen exit advisor — reviews open positions and cuts losers / harvests when curve needs it.

Runs on steward pass (rate-limited). Complements exchange TP/SL; does not replace them.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autonomous_engine import AutonomousGrowthEngine
    from config import Settings
    from exchange_client import BlofinExchange
    from ml.outcomes import TradeOutcomeTracker
    from position_registry import PositionRegistry

log = logging.getLogger(__name__)

_STATE = "llm_exit_advisor_ts.json"
_CACHE: dict[str, tuple[float, str]] = {}


def exit_advisor_active(settings: "Settings") -> bool:
    if not getattr(settings, "llm_exit_advisor", False):
        return False
    if not getattr(settings, "llm_overseer_mode", False):
        return False
    try:
        from local_llm import resolve_provider

        return resolve_provider() != "none"
    except Exception:
        return False


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE


def _due(state_dir: Path, interval: float) -> bool:
    path = _state_path(state_dir)
    if not path.is_file():
        return True
    try:
        ts = float(json.loads(path.read_text(encoding="utf-8")).get("ts") or 0)
        return (time.time() - ts) >= interval
    except Exception:
        return True


def _mark_ran(state_dir: Path) -> None:
    _state_path(state_dir).write_text(
        json.dumps({"ts": time.time()}, indent=2),
        encoding="utf-8",
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    try:
        return json.loads(t)
    except Exception:
        s, e = t.find("{"), t.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except Exception:
                return None
    return None


def _margin_roe(side: str, entry: float, price: float, lev: int) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    gross = (price - entry) / entry if side == "long" else (entry - price) / entry
    return gross * max(1, lev) * 100.0


def maybe_advise_exits(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: "PositionRegistry",
    engine: "AutonomousGrowthEngine",
    tracker: "TradeOutcomeTracker | None",
    *,
    harvest_eagerness: float = 1.0,
) -> int:
    """Return count of positions closed by Qwen exit advice."""
    if not positions or not exit_advisor_active(settings):
        return 0
    interval = float(getattr(settings, "llm_exit_advisor_interval_sec", 60.0))
    if not _due(settings.state_dir, interval):
        return 0

    from local_llm import chat_completion, resolve_provider, status_line

    if resolve_provider() == "none":
        return 0

    curve = getattr(engine, "_last_curve", None)
    preserve = bool(getattr(curve, "preserve_capital", False)) if curve else False
    vert = float(getattr(curve, "verticality", 0) or 0) if curve else 0.0

    rows: list[dict[str, Any]] = []
    for sym, pos in list(positions.items())[:4]:
        symbol = str(pos.get("symbol") or sym.split("#")[0])
        meta = registry.get(symbol) or registry.get(sym) or {}
        entry = float(pos.get("entry_price") or meta.get("entry_price") or 0)
        side = str(pos.get("side") or meta.get("side") or "long")
        last = (ex.stream.get_last_price(sym) if ex.stream else None) or entry
        lev = int(meta.get("leverage") or settings.scalp_leverage if settings.scalp_mode else settings.leverage)
        roe = _margin_roe(side, entry, last, lev)
        opened = float(meta.get("opened_at") or 0)
        age_min = (time.time() - opened) / 60.0 if opened else 0.0
        rows.append(
            {
                "symbol": symbol.split("/")[0],
                "side": side,
                "roe_pct": round(roe, 2),
                "age_min": round(age_min, 1),
                "stop_pct": float(meta.get("stop_pct") or 0),
                "take_pct": float(meta.get("take_pct") or 0),
            }
        )

    if not rows:
        return 0

    system = (
        "You are Qwen exit advisor for a Blofin scalper. Protect the account curve. "
        "Return ONLY JSON: "
        '{"decisions":[{"symbol":"BTC","action":"hold|harvest|cut","reason":"short"}]}. '
        "Rules: cut underwater chop losers (roe<-12% and age>3min); "
        "harvest winners early only if preserve_capital=true or harvest_eagerness>=0.85; "
        "hold runners near TP; never cut small green trades in stack_winners mode. "
        "Prefer hold when uncertain."
    )
    user = {
        "positions": rows,
        "curve": {"preserve_capital": preserve, "verticality": vert, "harvest_eagerness": harvest_eagerness},
        "stack_winners": getattr(settings, "stack_winners_mode", True),
        "llm": status_line(),
    }
    text, err = chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, separators=(",", ":"))},
        ],
        max_tokens=200,
        temperature=0.08,
        mode="policy",
    )
    _mark_ran(settings.state_dir)
    if not text:
        log.debug("exit advisor: %s", err)
        return 0

    blob = _parse_json(text)
    if not blob:
        return 0

    decisions = blob.get("decisions") or []
    if not isinstance(decisions, list):
        return 0

    from position_rotator import RotationAction, execute_rotation

    closed = 0
    sym_map = {str(r["symbol"]).upper(): r for r in rows}
    full_sym: dict[str, str] = {}
    for sym in positions:
        base = str(positions[sym].get("symbol") or sym).split("/")[0].upper()
        full_sym[base] = sym

    for dec in decisions[:4]:
        if not isinstance(dec, dict):
            continue
        base = str(dec.get("symbol") or "").split("/")[0].upper()
        action = str(dec.get("action") or "hold").lower()
        reason = str(dec.get("reason") or "qwen_exit")[:100]
        if action not in ("harvest", "cut") or base not in full_sym:
            continue
        sym = full_sym[base]
        row = sym_map.get(base, {})
        roe = float(row.get("roe_pct") or 0)
        if action == "cut" and roe > 5.0:
            continue
        if action == "harvest":
            if getattr(settings, "stack_winners_mode", True) and not preserve and harvest_eagerness < 0.82:
                continue
            if roe < 8.0:
                continue
        pos = positions.get(sym)
        if not pos:
            continue
        rot = RotationAction(
            symbol=sym,
            action="harvest",
            reason=f"qwen_{action}: {reason} roe={roe:.1f}%",
            pnl_after_fees_usd=0.0,
        )
        meta = registry.get(sym) or {}
        margin = float(meta.get("margin_usdt") or pos.get("margin") or 0)
        est_pnl = margin * roe / 100.0 if margin > 0 else rot.pnl_after_fees_usd
        if execute_rotation(ex, rot, positions, registry, settings.dry_run, tracker):
            engine.record_closed_trade(
                sym,
                est_pnl,
                side=str(pos.get("side") or ""),
                event=f"qwen_{action}",
                roe_pct=roe,
                entry=float(pos.get("entry_price") or meta.get("entry_price") or 0),
                leverage=int(meta.get("leverage") or 0) or None,
            )
            positions.pop(sym, None)
            closed += 1
            log.warning("QWEN EXIT %s %s — %s", base, action, reason)

    if closed:
        log.warning("QWEN exit advisor closed %d position(s)", closed)
    return closed
