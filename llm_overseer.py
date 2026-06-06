"""
LLM overseer — supervises ML swarm, rates universe, optimizes God Bot every 5 minutes.

- Instant asset ratings via universe_rater (no per-symbol LLM on entries).
- Every OVERSEER_INTERVAL_SECONDS (default 300): detect blockers, LLM tune gates,
  patch safe .env knobs, rewrite optimizer_overrides via autocode.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_llm import chat_completion, resolve_provider, status_line

log = logging.getLogger(__name__)

DIRECTIVES_FILE = "overseer_directives.json"
_last_cycle_ts = 0.0
_overseer_lock = threading.Lock()
_overseer_running = False
DEFAULT_INTERVAL_SEC = 300.0


@dataclass
class OverseerDirectives:
    conf_delta: float = 0.0
    score_delta: float = 0.0
    prefer: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    ml_mode: str = "neutral"
    notes: str = ""
    updated_ts: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OverseerDirectives":
        return cls(
            conf_delta=float(raw.get("conf_delta") or 0.0),
            score_delta=float(raw.get("score_delta") or 0.0),
            prefer=[str(x) for x in (raw.get("prefer") or [])][:12],
            avoid=[str(x) for x in (raw.get("avoid") or [])][:12],
            ml_mode=str(raw.get("ml_mode") or "neutral"),
            notes=str(raw.get("notes") or "")[:200],
            updated_ts=float(raw.get("updated_ts") or 0.0),
        )


def _directives_path(state_dir: Path) -> Path:
    return state_dir / DIRECTIVES_FILE


def load_directives(state_dir: Path) -> OverseerDirectives:
    path = _directives_path(state_dir)
    if not path.is_file():
        return OverseerDirectives()
    try:
        return OverseerDirectives.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return OverseerDirectives()


def save_directives(state_dir: Path, d: OverseerDirectives) -> None:
    path = _directives_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "conf_delta": d.conf_delta,
                "score_delta": d.score_delta,
                "prefer": d.prefer,
                "avoid": d.avoid,
                "ml_mode": d.ml_mode,
                "notes": d.notes,
                "updated_ts": d.updated_ts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def get_gate_adjustments(state_dir: Path) -> tuple[float, float]:
    d = load_directives(state_dir)
    return d.conf_delta, d.score_delta


def symbol_avoided(symbol: str, state_dir: Path) -> bool:
    d = load_directives(state_dir)
    base = symbol.split("/")[0].upper()
    for a in d.avoid:
        if a.upper() in symbol.upper() or base == a.upper():
            return True
    return False


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    try:
        return json.loads(t)
    except Exception:
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except Exception:
                return None
    return None


def _metrics_snapshot(state_dir: Path) -> dict[str, Any]:
    from universe_rater import load_ratings

    ratings = load_ratings(state_dir)
    wr, pf, streak = 0.5, 1.0, 0
    try:
        from roe_learning import get_roe_store

        wr, pf, streak, _ = get_roe_store(state_dir).recent_performance(3600.0, limit=30)
    except Exception:
        pass
    opens_60m = 0
    try:
        from scalp_optimizer import ScalpOptimizer
        from config import load_settings

        opt = ScalpOptimizer(state_dir, load_settings())
        opens_60m = int(getattr(opt.tuning, "opens_60m", 0) or 0)
    except Exception:
        pass
    return {
        "win_rate_1h": round(wr, 3),
        "profit_factor_1h": round(min(5.0, pf), 2),
        "loss_streak": streak,
        "opens_last_hour": opens_60m,
        "top_rated": [
            {
                "sym": r.get("symbol", "").split("/")[0],
                "tier": r.get("tier"),
                "composite": r.get("composite"),
                "chg24_pct": r.get("chg24_pct"),
            }
            for r in (ratings.get("top") or [])[:15]
        ],
        "bottom_rated": [
            {
                "sym": r.get("symbol", "").split("/")[0],
                "tier": r.get("tier"),
                "composite": r.get("composite"),
            }
            for r in (ratings.get("all") or [])[-8:]
        ],
    }


def _deterministic_fixes(blockers: dict[str, Any], settings: Any) -> dict[str, str]:
    """Pre-LLM fixes for known trade blockers (safe env only)."""
    fixes: dict[str, str] = {}
    issue_ids = {i.get("id") for i in blockers.get("issues") or []}

    if "llama_cpp_broken" in issue_ids or "llm_only_per_symbol" in issue_ids:
        fixes["LLM_ONLY_TRADING"] = "false"
        fixes["LLM_OVERSEER_MODE"] = "true"
        fixes["LLM_TRADING_ENABLED"] = "false"
        fixes["WHATSAPP_LLM_PROVIDER"] = "hf_local"
        fixes["WHATSAPP_LLM_SKIP_LLAMA"] = "true"
        fixes["SIGNAL_MODE"] = "ml"
        fixes["HOURLY_3R_WINNER_MODE"] = "true"
        fixes["WINNER_ONLY_MODE"] = "true"
        fixes["MOON_SWARM_ENABLED"] = "true"

    if "flow_starved" in issue_ids or "llm_approvals_no_fills" in issue_ids:
        fixes["OPTIMIZER_AUTOCODE_ENABLED"] = "true"
        cur = int(getattr(settings, "symbols_per_tick", 120))
        fixes["SYMBOLS_PER_TICK"] = str(min(200, max(cur, 120)))
        tph = int(getattr(settings, "optimizer_target_min_tph", 4))
        if tph < 4:
            fixes["OPTIMIZER_TARGET_MIN_TPH"] = "4"

    if "ml_not_ready" in issue_ids:
        fixes["ML_CONTINUOUS_TRAIN"] = "true"
        fixes["ML_AUTO_REFIT_ON_STARTUP"] = "true"

    if "entries_paused" in issue_ids or "knobs_pause" in issue_ids:
        fixes["ENTRIES_PAUSED"] = "false"

    return fixes


def _apply_env_fixes(root: Path, fixes: dict[str, str]) -> list[str]:
    if not fixes:
        return []
    from overseer_env import patch_env

    return patch_env(root / ".env", fixes)


def run_overseer_cycle(
    state_dir: Path,
    root: Path,
    blockers: dict[str, Any],
    settings: Any,
    *,
    force: bool = False,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
) -> OverseerDirectives | None:
    """LLM optimizes God Bot: gates, focus symbols, autocode, env patches."""
    global _last_cycle_ts
    now = time.time()
    if not force and (now - _last_cycle_ts) < interval_sec:
        return None

    det = _deterministic_fixes(blockers, settings)
    if det:
        applied = _apply_env_fixes(root, det)
        if applied:
            log.warning("OVERSEER auto-fix (deterministic): %s", ", ".join(applied))

    if resolve_provider() == "none":
        log.warning("OVERSEER: no LLM provider — deterministic fixes only")
        _last_cycle_ts = now
        return None

    metrics = _metrics_snapshot(state_dir)
    system = (
        "You optimize a crypto God Bot (ML signal + winner + pick + swarm + 15m optimizer). "
        "Foresee blockers and fix flow BEFORE trades starve. Return ONLY JSON: "
        '{"conf_delta":float,"score_delta":float,"prefer":["SYM"],"avoid":["SYM"],'
        '"ml_mode":"quality|throughput|neutral","env_fixes":{"KEY":"value"},'
        '"notes":"short"}. '
        "Bounds: conf_delta [-0.06,0.06], score_delta [-4,4]. "
        "env_fixes keys allowed: LLM_ONLY_TRADING, LLM_OVERSEER_MODE, SIGNAL_MODE, "
        "SYMBOLS_PER_TICK, OPTIMIZER_TARGET_MIN_TPH, ML_CONTINUOUS_TRAIN, ENTRIES_PAUSED, "
        "HOURLY_3R_WINNER_MODE, WINNER_ONLY_MODE, WHATSAPP_LLM_PROVIDER. "
        "If blockers show flow_starved or opens_last_hour<4: throughput mode, loosen gates, "
        "raise SYMBOLS_PER_TICK. If llm_only_per_symbol: set LLM_ONLY_TRADING=false. "
        "Never enable LLM_ONLY_TRADING."
    )
    payload = {
        "metrics": metrics,
        "blockers": blockers.get("issues") or [],
        "llm": status_line(),
        "settings": {
            "llm_only": getattr(settings, "llm_only_trading", False),
            "signal_mode": getattr(settings, "signal_mode", ""),
            "symbols_per_tick": getattr(settings, "symbols_per_tick", 0),
            "hourly_3r": getattr(settings, "hourly_3r_winner_mode", False),
        },
    }
    text, err = chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
        ],
        max_tokens=280,
        temperature=0.12,
        mode="policy",
    )
    if not text:
        log.warning("OVERSEER 5m cycle failed: %s", err or "empty")
        _last_cycle_ts = now
        return None

    blob = _parse_llm_json(text)
    if not blob:
        log.warning("OVERSEER: could not parse JSON")
        _last_cycle_ts = now
        return None

    env_fixes = blob.get("env_fixes") or {}
    if isinstance(env_fixes, dict) and env_fixes:
        llm_applied = _apply_env_fixes(root, {str(k): str(v) for k, v in env_fixes.items()})
        if llm_applied:
            log.warning("OVERSEER auto-fix (LLM): %s", ", ".join(llm_applied))

    d = OverseerDirectives.from_dict(
        {
            **blob,
            "conf_delta": max(-0.06, min(0.06, float(blob.get("conf_delta") or 0))),
            "score_delta": max(-4.0, min(4.0, float(blob.get("score_delta") or 0))),
            "updated_ts": now,
        }
    )
    save_directives(state_dir, d)
    _last_cycle_ts = now

    if d.ml_mode in ("quality", "throughput", "neutral"):
        try:
            from optimizer_autocode import maybe_apply_autocode

            action = (
                "loosen_throughput"
                if d.ml_mode == "throughput"
                else ("tighten_quality" if d.ml_mode == "quality" else "steady")
            )
            maybe_apply_autocode(
                state_dir,
                enabled=getattr(settings, "optimizer_autocode_enabled", True),
                action=action,
                win_rate=float(metrics["win_rate_1h"]),
                profit_factor=float(metrics["profit_factor_1h"]),
                equity_delta_15m_pct=0.0,
                trades_last_hour=int(metrics["opens_last_hour"]),
                cooldown_sec=max(240, int(interval_sec * 0.8)),
            )
        except Exception:
            log.debug("overseer autocode failed", exc_info=True)

    log.warning(
        "OVERSEER 5m optimize | conf%+.3f score%+.1f prefer=%s avoid=%s mode=%s | %s",
        d.conf_delta,
        d.score_delta,
        ",".join(d.prefer[:5]) or "-",
        ",".join(d.avoid[:5]) or "-",
        d.ml_mode,
        d.notes[:80],
    )
    return d


def maybe_run_overseer_tick(
    settings: Any,
    *,
    root: Path | None = None,
    knobs: Any = None,
    ml: Any = None,
    ml_trainer: Any = None,
    opens_allowed: bool = True,
) -> None:
    global _overseer_running
    if not getattr(settings, "llm_overseer_mode", False):
        return

    interval = float(getattr(settings, "overseer_interval_seconds", DEFAULT_INTERVAL_SEC))
    with _overseer_lock:
        if _overseer_running:
            return
        if (time.time() - _last_cycle_ts) < interval:
            return
        _overseer_running = True

    import api_backoff
    from trade_blockers import detect_blockers

    root = root or Path(__file__).resolve().parent
    opens_60m = 0
    try:
        from scalp_optimizer import ScalpOptimizer

        opt = ScalpOptimizer(settings.state_dir, settings)
        opens_60m = int(getattr(opt.tuning, "opens_60m", 0) or 0)
    except Exception:
        pass

    blockers = detect_blockers(
        settings.state_dir,
        settings.log_dir,
        settings=settings,
        knobs=knobs,
        ml_ready=bool(ml and ml.is_ready()),
        opens_last_hour=opens_60m,
        opens_allowed=opens_allowed,
        api_paused=api_backoff.is_paused(),
    )

    def _run() -> None:
        global _overseer_running
        try:
            run_overseer_cycle(
                settings.state_dir,
                root,
                blockers,
                settings,
                interval_sec=interval,
            )
        finally:
            with _overseer_lock:
                _overseer_running = False

    threading.Thread(target=_run, daemon=True, name="llm-overseer").start()
