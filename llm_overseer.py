"""
Qwen caretaker overseer — God Bot's built-in LLM with full supervisory mandate.

Every OVERSEER_INTERVAL_SECONDS (default 300): Qwen reads bot health, blockers,
and cortex memory; tunes gates, avoid/prefer symbols, patches safe .env knobs,
runs repairs (TP/SL, curve, stack), and triggers autocode.
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
PULSE_FILE = "overseer_pulse.json"
_last_cycle_ts = 0.0
_overseer_lock = threading.Lock()
_overseer_running = False
DEFAULT_INTERVAL_SEC = 300.0
MIN_INTERVAL_SEC = 45.0


@dataclass
class OverseerDirectives:
    conf_delta: float = 0.0
    score_delta: float = 0.0
    pick_min_delta: float = 0.0
    prefer: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    ml_mode: str = "neutral"
    winner_tier_floor: str = "good"
    elite_only: bool = False
    notes: str = ""
    updated_ts: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OverseerDirectives":
        tier = str(raw.get("winner_tier_floor") or "good").lower()
        if tier not in ("good", "elite", "apex"):
            tier = "good"
        return cls(
            conf_delta=float(raw.get("conf_delta") or 0.0),
            score_delta=float(raw.get("score_delta") or 0.0),
            pick_min_delta=float(raw.get("pick_min_delta") or 0.0),
            prefer=[str(x) for x in (raw.get("prefer") or [])][:12],
            avoid=[str(x) for x in (raw.get("avoid") or [])][:12],
            ml_mode=str(raw.get("ml_mode") or "neutral"),
            winner_tier_floor=tier,
            elite_only=bool(raw.get("elite_only")),
            notes=str(raw.get("notes") or "")[:200],
            updated_ts=float(raw.get("updated_ts") or 0.0),
        )


def _directives_path(state_dir: Path) -> Path:
    return state_dir / DIRECTIVES_FILE


def _pulse_path(state_dir: Path) -> Path:
    return state_dir / PULSE_FILE


def pulse_overseer(state_dir: Path, reason: str) -> None:
    """Request Qwen caretaker cycle ASAP (e.g. after a loss or flat curve)."""
    path = _pulse_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prev = {}
        if path.is_file():
            prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    path.write_text(
        json.dumps(
            {
                "requested_ts": time.time(),
                "reason": str(reason)[:120],
                "count": int(prev.get("count") or 0) + 1,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _pulse_pending(state_dir: Path, *, max_age_sec: float = 120.0) -> tuple[bool, str]:
    path = _pulse_path(state_dir)
    if not path.is_file():
        return False, ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = float(raw.get("requested_ts") or 0)
        if time.time() - ts > max_age_sec:
            return False, ""
        return True, str(raw.get("reason") or "")
    except Exception:
        return False, ""


def _clear_pulse(state_dir: Path) -> None:
    path = _pulse_path(state_dir)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def overseer_interval_for_state(state_dir: Path, settings: Any) -> float:
    """
    Adaptive Qwen cadence — faster when curve is sick or losers stack.
    Default OVERSEER_INTERVAL_SECONDS when healthy.
    """
    base = float(getattr(settings, "overseer_interval_seconds", DEFAULT_INTERVAL_SEC))
    wr, pf, streak = 0.5, 1.0, 0
    try:
        from roe_learning import get_roe_store

        wr, pf, streak, _ = get_roe_store(state_dir).recent_performance(3600.0, limit=30)
    except Exception:
        pass
    health = _caretaker_health(state_dir)
    vert = float(health.get("curve_verticality") or 0)
    dd = float(health.get("drawdown_from_peak_pct") or 0)
    phase = str(health.get("curve_phase") or "")

    if streak >= 4 or (streak >= 3 and pf < 0.95):
        return max(MIN_INTERVAL_SEC, min(base, 60.0))
    if streak >= 2 or wr < 0.40 or pf < 0.90:
        return max(MIN_INTERVAL_SEC, min(base, 90.0))
    if dd >= 12.0 or phase in ("preserve", "stress", "recover") or vert < 0.25:
        return max(MIN_INTERVAL_SEC, min(base, 120.0))
    if wr >= 0.52 and pf >= 1.12 and streak < 2 and vert >= 0.45:
        return base
    return max(MIN_INTERVAL_SEC, min(base, 180.0))


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
                "pick_min_delta": d.pick_min_delta,
                "prefer": d.prefer,
                "avoid": d.avoid,
                "ml_mode": d.ml_mode,
                "winner_tier_floor": d.winner_tier_floor,
                "elite_only": d.elite_only,
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


def get_winner_adjustments(state_dir: Path) -> OverseerDirectives:
    """Winner-pick knobs for pick_engine / winner_gate."""
    return load_directives(state_dir)


def overseer_elite_only(state_dir: Path) -> bool:
    d = load_directives(state_dir)
    return bool(d.elite_only or d.winner_tier_floor in ("elite", "apex"))


def overseer_min_winner_tier(state_dir: Path) -> str:
    d = load_directives(state_dir)
    if d.elite_only:
        return "elite"
    return d.winner_tier_floor if d.winner_tier_floor in ("good", "elite", "apex") else "good"


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


def _cold_hot_symbols(state_dir: Path) -> tuple[list[str], list[str]]:
    cold: list[str] = []
    hot: list[str] = []
    try:
        from roe_learning import get_roe_store

        recent = list((get_roe_store(state_dir)._data.get("global") or {}).get("recent") or [])[-24:]
        for row in recent:
            sym = str(row.get("symbol") or "").split("/")[0].upper()
            if not sym:
                continue
            roe = float(row.get("roe_pct") or 0)
            if roe < -8.0 and sym not in cold:
                cold.append(sym)
            elif roe > 12.0 and sym not in hot:
                hot.append(sym)
    except Exception:
        pass
    return cold[:10], hot[:10]


def _caretaker_health(state_dir: Path) -> dict[str, Any]:
    """Bot vitals for Qwen — equity, curve, pauses, open risk."""
    health: dict[str, Any] = {}
    try:
        snap = json.loads((state_dir / "account_snapshot.json").read_text(encoding="utf-8"))
        health["equity"] = round(float(snap.get("equity") or 0), 2)
        health["open_positions"] = int(snap.get("open_count") or snap.get("positions") or 0)
    except Exception:
        health["equity"] = 0.0
        health["open_positions"] = 0
    try:
        curve = json.loads((state_dir / "pnl_curve.json").read_text(encoding="utf-8"))
        health["curve_verticality"] = round(float(curve.get("last_verticality") or 0), 3)
        health["curve_phase"] = str(curve.get("last_phase") or "")
        health["drawdown_from_peak_pct"] = round(float(curve.get("drawdown_from_peak_pct") or 0), 2)
    except Exception:
        pass
    try:
        from runtime_gates import read_entries_pause

        paused, reason = read_entries_pause(state_dir)
        health["entries_paused_runtime"] = paused
        health["pause_reason"] = reason
    except Exception:
        health["entries_paused_runtime"] = False
    try:
        from local_cortex import knowledge_block

        kb = knowledge_block(400)
        health["cortex_chars"] = len(kb or "")
    except Exception:
        health["cortex_chars"] = 0
    return health


def _qwen_caretaker_prompt() -> str:
    return (
        "You are Qwen — the living caretaker of God Bot (Blofin USDT perpetual scalper). "
        "You have FULL responsibility: optimize gates, pick winners, keep the stack healthy, "
        "fix anything broken, and protect the account curve. ML swarm + winner gate + pick engine "
        "execute entries; YOU supervise, tune, veto symbols, patch config, and run repairs. "
        "Your cortex learns from every close — use hot/cold symbols and loss_streak. "
        "MISSION: steep vertical account curve; more winners; cut loser strings fast. "
        "On loss_streak>=3 or win_rate_1h<0.42: quality mode, elite tier, avoid cold symbols, "
        "consider ENTRIES_PAUSED=true briefly (15–30 min) only if streak>=5 and PF<0.9. "
        "When healthy (WR>=0.50, PF>=1.1, streak<2): loosen slightly if flow_starved. "
        "Keep bot RUNNING CLEAN: clear false pauses, enable ML_CONTINUOUS_TRAIN if ml_not_ready, "
        "repair TP/SL if tpsl_repair blocker, stack_ensure if dashboard/bot stale. "
        "Never enable LLM_ONLY_TRADING. "
        "Return ONLY JSON: "
        '{"conf_delta":float,"score_delta":float,"pick_min_delta":float,'
        '"prefer":["SYM"],"avoid":["SYM"],'
        '"ml_mode":"quality|throughput|neutral","winner_tier_floor":"good|elite|apex",'
        '"elite_only":bool,'
        '"env_fixes":{"KEY":"value"},'
        '"actions":["cortex_train"|"repair_tpsl"|"stack_ensure"|"clear_pause"|"curve_repair"|"ml_refit"],'
        '"notes":"what you did and why"}. '
        "Bounds: conf_delta [-0.02,0.06], score_delta [-2,6], pick_min_delta [0,0.10]. "
        "env_fixes allowlist: QUALITY_PICK_MODE, WINNER_ONLY_MODE, WINNER_ELITE_ONLY, "
        "LLM_COPILOT_TRADING, LLM_COPILOT_STRICT, SYMBOLS_PER_TICK, OPTIMIZER_TARGET_MIN_TPH, "
        "ENTRIES_PAUSED, HOURLY_3R_WINNER_MODE, ML_CONTINUOUS_TRAIN, OPTIMIZER_AUTOCODE_ENABLED."
    )


def _execute_overseer_actions(
    actions: list[str],
    *,
    state_dir: Path,
    root: Path,
    settings: Any,
) -> list[str]:
    """Run allowlisted caretaker repairs Qwen requests in actions[]."""
    import subprocess
    import sys

    allowed = frozenset(
        {
            "cortex_train",
            "repair_tpsl",
            "stack_ensure",
            "clear_pause",
            "curve_repair",
            "ml_refit",
        }
    )
    done: list[str] = []
    for raw in actions[:6]:
        action = str(raw).strip().lower()
        if action not in allowed:
            continue
        try:
            if action == "cortex_train":
                from local_cortex import train

                summary = train(state_dir)
                done.append(f"cortex_train:{summary.get('examples', 0)}")
            elif action == "clear_pause":
                from runtime_gates import clear_entries_pause

                clear_entries_pause(state_dir)
                done.append("clear_pause")
            elif action == "ml_refit":
                (state_dir / "ml_force_refit.flag").write_text(
                    json.dumps({"reason": "qwen_overseer"}, indent=2),
                    encoding="utf-8",
                )
                done.append("ml_refit")
            elif action == "curve_repair":
                from curve_guard import repair_equity_curve, fetch_live_equity

                eq, _ = fetch_live_equity(state_dir)
                repair_equity_curve(state_dir, live_equity=eq if eq > 0 else None)
                done.append("curve_repair")
            elif action == "repair_tpsl":
                script = root / "scripts" / "repair_open_tpsl.py"
                if script.is_file():
                    subprocess.run(
                        [sys.executable, str(script)],
                        cwd=str(root),
                        check=False,
                        timeout=120,
                    )
                    done.append("repair_tpsl")
            elif action == "stack_ensure":
                ps1 = root / "scripts" / "stack_control.ps1"
                if ps1.is_file():
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
                    subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(ps1),
                            "-Action",
                            "ensure",
                        ],
                        cwd=str(root),
                        check=False,
                        timeout=90,
                        creationflags=flags,
                    )
                    done.append("stack_ensure")
        except Exception as exc:
            log.warning("OVERSEER action %s failed: %s", action, exc)
    return done


def _metrics_snapshot(state_dir: Path) -> dict[str, Any]:
    from universe_rater import load_ratings

    ratings = load_ratings(state_dir)
    wr, pf, streak, avg_roe = 0.5, 1.0, 0, 0.0
    try:
        from roe_learning import get_roe_store

        wr, pf, streak, avg_roe = get_roe_store(state_dir).recent_performance(3600.0, limit=30)
    except Exception:
        pass
    cold, hot = _cold_hot_symbols(state_dir)
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
        "avg_roe_1h": round(float(avg_roe), 2),
        "loss_streak": streak,
        "opens_last_hour": opens_60m,
        "cold_symbols_recent": cold,
        "hot_symbols_recent": hot,
        "bot_health": _caretaker_health(state_dir),
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


def _deterministic_winner_directives(
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    """Tighten winner bar when live WR/PF is weak — before LLM runs."""
    wr = float(metrics.get("win_rate_1h") or 0.5)
    pf = float(metrics.get("profit_factor_1h") or 1.0)
    streak = int(metrics.get("loss_streak") or 0)
    cold = list(metrics.get("cold_symbols_recent") or [])
    hot = list(metrics.get("hot_symbols_recent") or [])
    top = [r.get("sym") for r in (metrics.get("top_rated") or [])[:6] if r.get("sym")]

    if wr >= 0.48 and pf >= 1.05 and streak < 3:
        return None

    prefer = list(dict.fromkeys(hot + top))[:10]
    avoid = cold[:10]
    if wr < 0.38 or pf < 0.85 or streak >= 4:
        blob: dict[str, Any] = {
            "conf_delta": 0.04,
            "score_delta": 3.0,
            "pick_min_delta": 0.06,
            "prefer": prefer,
            "avoid": avoid,
            "ml_mode": "quality",
            "winner_tier_floor": "elite",
            "elite_only": True,
            "actions": ["cortex_train"],
            "notes": f"auto quality: wr={wr:.0%} pf={pf:.2f} streak={streak}",
        }
        if streak >= 5:
            blob["actions"] = ["cortex_train", "curve_repair"]
        return blob
    if wr < 0.45 or pf < 0.95 or streak >= 2:
        return {
            "conf_delta": 0.02,
            "score_delta": 2.0,
            "pick_min_delta": 0.04,
            "prefer": prefer,
            "avoid": avoid,
            "ml_mode": "quality",
            "winner_tier_floor": "elite",
            "elite_only": False,
            "notes": f"auto tighten: wr={wr:.0%} pf={pf:.2f} streak={streak}",
        }
    return None


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

    metrics = _metrics_snapshot(state_dir)
    auto_winner = _deterministic_winner_directives(metrics)
    if auto_winner:
        d_auto = OverseerDirectives.from_dict({**auto_winner, "updated_ts": now})
        save_directives(state_dir, d_auto)
        env_auto = {
            "QUALITY_PICK_MODE": "true",
            "WINNER_ONLY_MODE": "true",
            "LLM_COPILOT_TRADING": "true",
        }
        if d_auto.elite_only:
            env_auto["WINNER_ELITE_ONLY"] = "true"
        applied = _apply_env_fixes(root, env_auto)
        if applied:
            log.warning("OVERSEER winner-tighten (deterministic): %s", ", ".join(applied))
        auto_actions = auto_winner.get("actions") or []
        if isinstance(auto_actions, list) and auto_actions:
            _execute_overseer_actions(
                [str(a) for a in auto_actions],
                state_dir=state_dir,
                root=root,
                settings=settings,
            )

    if resolve_provider() == "none":
        log.warning("OVERSEER: no LLM provider — deterministic fixes only")
        _last_cycle_ts = now
        return load_directives(state_dir) if auto_winner else None

    system = _qwen_caretaker_prompt()
    payload = {
        "role": "You are Qwen. This is your God Bot — optimize it and keep it healthy.",
        "metrics": metrics,
        "blockers": blockers.get("issues") or [],
        "llm_backend": status_line(),
        "settings": {
            "llm_only": getattr(settings, "llm_only_trading", False),
            "llm_copilot": getattr(settings, "llm_copilot_trading", False),
            "signal_mode": getattr(settings, "signal_mode", ""),
            "symbols_per_tick": getattr(settings, "symbols_per_tick", 0),
            "hourly_3r": getattr(settings, "hourly_3r_winner_mode", False),
            "entries_paused_env": getattr(settings, "entries_paused", False),
            "quality_pick": getattr(settings, "quality_pick_mode", False),
            "winner_only": getattr(settings, "winner_only_mode", False),
        },
    }
    text, err = chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
        ],
        max_tokens=380,
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
            log.warning("QWEN caretaker env: %s", ", ".join(llm_applied))

    raw_actions = blob.get("actions") or []
    if isinstance(raw_actions, list) and raw_actions:
        action_log = _execute_overseer_actions(
            [str(a) for a in raw_actions],
            state_dir=state_dir,
            root=root,
            settings=settings,
        )
        if action_log:
            log.warning("QWEN caretaker actions: %s", ", ".join(action_log))

    merged = {**(auto_winner or {}), **blob}
    d = OverseerDirectives.from_dict(
        {
            **merged,
            "conf_delta": max(-0.02, min(0.06, float(merged.get("conf_delta") or 0))),
            "score_delta": max(-2.0, min(6.0, float(merged.get("score_delta") or 0))),
            "pick_min_delta": max(0.0, min(0.10, float(merged.get("pick_min_delta") or 0))),
            "updated_ts": now,
        }
    )
    if d.ml_mode == "quality" or d.elite_only:
        _apply_env_fixes(
            root,
            {
                "QUALITY_PICK_MODE": "true",
                "WINNER_ONLY_MODE": "true",
                "LLM_COPILOT_TRADING": "true",
                **({"WINNER_ELITE_ONLY": "true"} if d.elite_only else {}),
            },
        )
    save_directives(state_dir, d)
    _last_cycle_ts = now
    _clear_pulse(state_dir)

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
        "QWEN caretaker | conf%+.3f score%+.1f pick%+.2f tier=%s elite=%s "
        "prefer=%s avoid=%s mode=%s | %s",
        d.conf_delta,
        d.score_delta,
        d.pick_min_delta,
        d.winner_tier_floor,
        d.elite_only,
        ",".join(d.prefer[:5]) or "-",
        ",".join(d.avoid[:5]) or "-",
        d.ml_mode,
        d.notes[:120],
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

    interval = overseer_interval_for_state(settings.state_dir, settings)
    pulsed, pulse_reason = _pulse_pending(settings.state_dir)
    with _overseer_lock:
        if _overseer_running:
            return
        elapsed = time.time() - _last_cycle_ts
        if not pulsed and elapsed < interval:
            return
        _overseer_running = True
    if pulsed:
        log.warning("QWEN pulse: %s (interval=%ds)", pulse_reason or "distress", int(interval))

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
