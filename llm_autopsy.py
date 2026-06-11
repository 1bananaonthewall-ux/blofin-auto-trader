"""
Qwen post-close autopsy — learns from recent closes and updates overseer avoid/prefer.

Runs every ~10 min while bot is live; feeds cortex + directives (no lookahead on entries).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Settings

log = logging.getLogger(__name__)

_STATE = "llm_autopsy_ts.json"
_lock = threading.Lock()
_running = False


def autopsy_active(settings: "Settings") -> bool:
    if not getattr(settings, "llm_autopsy_enabled", False):
        return False
    if not getattr(settings, "llm_overseer_mode", False):
        return False
    try:
        from local_llm import resolve_provider

        return resolve_provider() != "none"
    except Exception:
        return False


def _recent_closes(state_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    path = state_dir / "trade_outcomes.jsonl"
    if not path.is_file():
        path = state_dir / "profitability.json"
        if path.is_file():
            try:
                trades = json.loads(path.read_text(encoding="utf-8")).get("trades") or []
                return [
                    {
                        "symbol": str(t.get("symbol") or "").split("/")[0],
                        "side": t.get("side", ""),
                        "roe_pct": t.get("roe_pct"),
                        "pnl_usd": t.get("net_pnl"),
                        "result": "win" if float(t.get("net_pnl") or 0) > 0 else "loss",
                    }
                    for t in trades[-limit:]
                ]
            except Exception:
                return []
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()[-400:]):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            rows.append(
                {
                    "symbol": str(row.get("symbol") or "").split("/")[0],
                    "side": row.get("side", ""),
                    "roe_pct": row.get("roe_pct"),
                    "pnl_usd": row.get("pnl_usd"),
                    "result": "win"
                    if row.get("outcome") == "win" or int(row.get("win", 0) or 0) == 1
                    else "loss",
                }
            )
            if len(rows) >= limit:
                break
    except Exception:
        pass
    return list(reversed(rows))


def run_autopsy_cycle(
    state_dir: Path,
    settings: "Settings",
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    if not autopsy_active(settings):
        return None
    interval = float(getattr(settings, "llm_autopsy_interval_sec", 600.0))
    state_path = state_dir / _STATE
    now = time.time()
    if not force and state_path.is_file():
        try:
            last = float(json.loads(state_path.read_text(encoding="utf-8")).get("ts") or 0)
            if now - last < interval:
                return None
        except Exception:
            pass

    closes = _recent_closes(state_dir)
    if len(closes) < 3:
        return None

    from local_llm import chat_completion, resolve_provider, status_line
    from llm_overseer import load_directives, save_directives, OverseerDirectives

    if resolve_provider() == "none":
        return None

    try:
        from local_cortex import train

        train(state_dir)
    except Exception:
        pass

    system = (
        "You are Qwen autopsy brain. Review recent closed trades and update God Bot focus. "
        "Return ONLY JSON: "
        '{"avoid":["SYM"],"prefer":["SYM"],"lesson":"one sentence","pick_note":"optional"}. '
        "Put repeat losers in avoid; repeat winners in prefer; be specific not generic."
    )
    user = {
        "recent_closes": closes,
        "llm": status_line(),
        "current_directives": {
            "avoid": load_directives(state_dir).avoid[:8],
            "prefer": load_directives(state_dir).prefer[:8],
        },
    }
    text, err = chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, separators=(",", ":"))},
        ],
        max_tokens=220,
        temperature=0.10,
        mode="policy",
    )
    state_path.write_text(json.dumps({"ts": now}, indent=2), encoding="utf-8")
    if not text:
        log.debug("autopsy failed: %s", err)
        return None

    try:
        s, e = text.find("{"), text.rfind("}")
        blob = json.loads(text[s : e + 1]) if s >= 0 else {}
    except Exception:
        return None

    avoid = [str(x).split("/")[0].upper() for x in (blob.get("avoid") or []) if x][:12]
    prefer = [str(x).split("/")[0].upper() for x in (blob.get("prefer") or []) if x][:12]
    lesson = str(blob.get("lesson") or "")[:160]

    cur = load_directives(state_dir)
    merged_avoid = list(dict.fromkeys(avoid + cur.avoid))[:12]
    merged_prefer = list(dict.fromkeys(prefer + cur.prefer))[:12]
    save_directives(
        state_dir,
        OverseerDirectives(
            conf_delta=cur.conf_delta,
            score_delta=cur.score_delta,
            pick_min_delta=cur.pick_min_delta,
            prefer=merged_prefer,
            avoid=merged_avoid,
            ml_mode=cur.ml_mode,
            winner_tier_floor=cur.winner_tier_floor,
            elite_only=cur.elite_only,
            notes=lesson or cur.notes,
            updated_ts=now,
        ),
    )
    log.warning(
        "QWEN autopsy | avoid=%s prefer=%s | %s",
        ",".join(merged_avoid[:5]) or "-",
        ",".join(merged_prefer[:5]) or "-",
        lesson[:80],
    )
    return {"avoid": merged_avoid, "prefer": merged_prefer, "lesson": lesson}


def maybe_run_autopsy_tick(settings: "Settings") -> None:
    global _running
    if not autopsy_active(settings):
        return
    with _lock:
        if _running:
            return
        _running = True

    def _run() -> None:
        global _running
        try:
            run_autopsy_cycle(settings.state_dir, settings)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="llm-autopsy").start()
