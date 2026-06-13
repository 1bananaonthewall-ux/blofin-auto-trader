"""Winner-first entry policy — keep leverage/flow; never loosen gates when starved."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

TUNING_PATH_NAME = "scalp_tuning.json"


def quality_pick_active(settings: "Settings") -> bool:
    return bool(getattr(settings, "quality_pick_mode", True))


def apply_quality_pick_boot(settings: "Settings") -> None:
    """Reset learned loosening so quality mode starts from base gates."""
    if not quality_pick_active(settings):
        return
    path = settings.state_dir / TUNING_PATH_NAME
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for key in ("confluence_delta", "ml_conf_delta", "min_score_delta"):
        v = float(raw.get(key) or 0)
        if v < 0:
            raw[key] = 0.0
            changed = True
    if int(raw.get("agreeing_delta") or 0) < 0:
        raw["agreeing_delta"] = 0
        changed = True
    if changed:
        raw["action"] = "quality_pick_reset"
        raw["notes"] = "quality_pick_mode — cleared gate loosen deltas"
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        log.warning("quality_pick: reset negative optimizer gate deltas in %s", path.name)


def live_performance(settings: "Settings", window_sec: float = 3600.0) -> tuple[float, float]:
    """Recent win rate and profit factor from ROE closes."""
    try:
        from roe_learning import get_roe_store

        wr, pf, _, _ = get_roe_store(settings.state_dir).recent_performance(window_sec)
        return float(wr), float(pf)
    except Exception:
        return 0.5, 1.0


def quality_conf_score_floors(settings: "Settings") -> tuple[float, float]:
    """Raise minimum conf/score when live WR is weak (never lower below base)."""
    if not quality_pick_active(settings):
        return 0.0, 0.0
    wr, pf = live_performance(settings)
    if wr < 0.38 or pf < 0.80:
        return 0.60, 58.0
    if wr < 0.42 or pf < 0.90:
        return 0.58, 56.0
    if wr < 0.46 or pf < 1.0:
        return 0.56, 54.0
    return 0.0, 0.0


def apply_quality_gates(
    settings: "Settings",
    conf_gate: float,
    score_gate: float,
) -> tuple[float, float]:
    """Use the higher of base gates and live-WR floors; never cap gates down."""
    floor_conf, floor_score = quality_conf_score_floors(settings)
    if floor_conf > 0:
        conf_gate = max(conf_gate, floor_conf)
        score_gate = max(score_gate, floor_score)
    return conf_gate, score_gate


def symbol_entry_blocked(settings: "Settings", symbol: str) -> tuple[bool, str]:
    """Symbol-level ROE block before scan (either side)."""
    if not quality_pick_active(settings):
        return False, ""
    try:
        from roe_learning import get_roe_store

        store = get_roe_store(settings.state_dir)
        sym_row = (store._data.get("symbols") or {}).get(symbol) or {}
    except Exception:
        return False, ""

    closes = int(sym_row.get("closes") or 0)
    wins = int(sym_row.get("wins") or 0)
    losses = int(sym_row.get("losses") or 0)
    ema = store.symbol_roe_ema(symbol)

    if closes >= 4 and wins / closes < 0.28:
        return True, f"symbol wr {wins}/{closes} too low"
    if ema is not None and ema <= -14.0 and closes >= 3:
        return True, f"symbol roe_ema {ema:+.1f}%"
    if losses >= 3 and wins == 0 and closes >= 3:
        return True, f"symbol {losses} losses 0 wins"
    return False, ""




def choppy_side_blocked(settings: "Settings", symbol: str, side: str) -> tuple[bool, str]:
    """Block symbol/side after repeated choppy-entry losses."""
    if not quality_pick_active(settings):
        return False, ""
    try:
        from roe_learning import get_roe_store

        store = get_roe_store(settings.state_dir)
        side_key = str(side).lower()
        recent = [
            r
            for r in (store._data.get("global", {}).get("recent") or [])
            if str(r.get("symbol") or "") == symbol
            and str(r.get("side") or "").lower() == side_key
        ][-4:]
    except Exception:
        return False, ""
    if len(recent) < 2:
        return False, ""
    chop_losses = sum(
        1
        for r in recent
        if float(r.get("roe_pct") or 0) < 0 and abs(float(r.get("roe_pct") or 0)) >= 12.0
    )
    if chop_losses >= 2:
        return True, f"{side_key} on {symbol.split('/')[0]} repeated chop losses"
    return False, ""

def entry_blocked_by_live_roe(
    settings: "Settings",
    symbol: str,
    side: str,
) -> tuple[bool, str]:
    """Block symbols/sides with repeated live losses."""
    if not quality_pick_active(settings):
        return False, ""
    try:
        from roe_learning import get_roe_store

        store = get_roe_store(settings.state_dir)
        data = store._data
    except Exception:
        return False, ""

    sym_row = (data.get("symbols") or {}).get(symbol) or {}
    closes = int(sym_row.get("closes") or 0)
    wins = int(sym_row.get("wins") or 0)
    losses = int(sym_row.get("losses") or 0)
    ema = store.symbol_roe_ema(symbol)

    blocked, reason = symbol_entry_blocked(settings, symbol)
    if blocked:
        return True, reason

    chop_blocked, chop_reason = choppy_side_blocked(settings, symbol, side)
    if chop_blocked:
        return True, chop_reason

    try:
        from forward_pick import symbol_forward_blocked

        fwd_blocked, fwd_reason = symbol_forward_blocked(
            settings.state_dir, symbol, str(side).lower()
        )
        if fwd_blocked:
            return True, fwd_reason
    except Exception:
        pass

    side_key = str(side).lower()
    recent = [
        r
        for r in (data.get("global", {}).get("recent") or [])
        if str(r.get("symbol") or "") == symbol and str(r.get("side") or "").lower() == side_key
    ][-6:]
    if len(recent) >= 3:
        side_wins = sum(1 for r in recent if float(r.get("roe_pct") or 0) > 0)
        if side_wins == 0:
            return True, f"{side_key} on {symbol.split('/')[0]} 0/{len(recent)} recent wins"
        if side_wins / len(recent) < 0.25 and len(recent) >= 4:
            return True, f"{side_key} on {symbol.split('/')[0]} wr {side_wins}/{len(recent)}"

    return False, ""
