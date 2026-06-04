"""
Dashboard Copilot — local LLM chat for the God Bot dashboard.

- Never places or sizes trades (engine-only).
- May edit project code when you ask (hot-reloaded via live_update).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
LOG_FILE = ROOT / "logs" / "bot.log"
CHAT_HISTORY = STATE_DIR / "dashboard_chat_history.jsonl"
EDIT_LOG = STATE_DIR / "copilot_edits.jsonl"
MAX_HISTORY = 24
MAX_REPLY_CHARS = 8000
UI_HISTORY_LIMIT = 20
_CORTEX_TRAIN_INTERVAL = 3600.0
_LAST_CORTEX_TRAIN = 0.0

_LLM_WARM_LOCK = threading.Lock()
_LLM_WARM_EVENT = threading.Event()
_LLM_KEEPER_STARTED = False
_LLM_STATE: dict[str, Any] = {
    "status": "idle",
    "detail": "",
    "ready_since": 0.0,
    "last_ping": 0.0,
    "last_error": "",
}


def copilot_llm_timeout_sec() -> int:
    return int(os.environ.get("DASHBOARD_LLM_TIMEOUT_SEC", "180"))


def copilot_llm_warmup_wait_sec() -> int:
    return int(os.environ.get("DASHBOARD_LLM_WARMUP_WAIT_SEC", "300"))


def copilot_llm_keepalive_sec() -> int:
    return int(os.environ.get("DASHBOARD_LLM_KEEPALIVE_SEC", "600"))


def get_copilot_llm_status() -> dict[str, Any]:
    from local_llm import resolve_provider, status_line

    prov = resolve_provider()
    return {
        "provider": prov,
        "status": _LLM_STATE.get("status", "idle"),
        "detail": _LLM_STATE.get("detail") or status_line(),
        "ready_since": _LLM_STATE.get("ready_since") or 0.0,
        "last_ping": _LLM_STATE.get("last_ping") or 0.0,
        "last_error": _LLM_STATE.get("last_error") or "",
        "timeout_sec": copilot_llm_timeout_sec(),
        "keepalive_sec": copilot_llm_keepalive_sec(),
    }


def _set_llm_state(**kwargs: Any) -> None:
    with _LLM_WARM_LOCK:
        _LLM_STATE.update(kwargs)


def _run_copilot_warmup() -> bool:
    from local_llm import resolve_provider, status_line, warmup_provider

    prov = resolve_provider()
    if prov == "none":
        _set_llm_state(status="none", detail=status_line(), last_error="no_local_llm")
        _LLM_WARM_EVENT.set()
        return False
    _set_llm_state(status="warming", detail=status_line(), last_error="")
    _LLM_WARM_EVENT.clear()
    try:
        line = warmup_provider()
        _set_llm_state(
            status="ready",
            detail=line,
            ready_since=time.time(),
            last_error="",
        )
        log.info("dashboard copilot LLM ready: %s", line)
        return True
    except Exception as exc:
        log.warning("dashboard copilot LLM warmup failed: %s", exc)
        _set_llm_state(status="error", detail=status_line(), last_error=str(exc)[:400])
        return False
    finally:
        _LLM_WARM_EVENT.set()


def _copilot_keepalive_ping() -> None:
    from local_llm import chat_completion, resolve_provider

    if resolve_provider() == "none":
        return
    chat_completion(
        [{"role": "user", "content": "ok"}],
        max_tokens=4,
        temperature=0.1,
        mode="chat",
    )
    _set_llm_state(last_ping=time.time())


def _copilot_llm_keeper_loop() -> None:
    _run_copilot_warmup()
    interval = max(120, copilot_llm_keepalive_sec())
    while True:
        time.sleep(float(interval))
        if _LLM_STATE.get("status") != "ready":
            _run_copilot_warmup()
            continue
        try:
            _copilot_keepalive_ping()
            log.debug("dashboard copilot LLM keepalive ok")
        except Exception as exc:
            log.warning("dashboard copilot keepalive failed: %s", exc)
            _set_llm_state(status="warming", last_error=str(exc)[:200])
            _LLM_WARM_EVENT.clear()
            _run_copilot_warmup()


def start_copilot_llm_keeper() -> None:
    """Load local LLM at dashboard startup and ping periodically to stay hot."""
    global _LLM_KEEPER_STARTED
    with _LLM_WARM_LOCK:
        if _LLM_KEEPER_STARTED:
            return
        _LLM_KEEPER_STARTED = True
    threading.Thread(
        target=_copilot_llm_keeper_loop,
        daemon=True,
        name="copilot-llm-keeper",
    ).start()


def ensure_copilot_llm_ready(*, block: bool = True) -> bool:
    """Wait until startup warmup finished (or failed)."""
    if _LLM_STATE.get("status") == "ready":
        return True
    if not block:
        return False
    if not _LLM_WARM_EVENT.wait(timeout=float(copilot_llm_warmup_wait_sec())):
        log.warning(
            "dashboard copilot still warming after %ss",
            copilot_llm_warmup_wait_sec(),
        )
        return False
    return _LLM_STATE.get("status") == "ready"

_BLOCKED_PARTS = frozenset(
    {".git", "state", "logs", ".venv", "node_modules", "__pycache__", ".cursor", "treasury"}
)
_ALLOWED_SUFFIXES = frozenset({".py", ".md", ".json", ".tsx", ".ts", ".css", ".ps1", ".env"})
_ENV_NAMES = frozenset({".env", ".env.example"})

_LIVE_TRADE_ORDER = re.compile(
    r"^\s*(open|close|buy|sell|go)\s+[\w/]+",
    re.I,
)
_EDIT_BLOCK = re.compile(
    r"```edit\s+path=([^\s\n`]+)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_PICK_PAT = re.compile(
    r"PICK\s+(\S+)\s+(long|short)\s+score=([\d.]+)\s+fast=([\d.]+)\s+tier=(\S+)",
    re.I,
)
_CONFLUENCE_PAT = re.compile(
    r"CONFLUENCE\s+(\S+)\s+(long|short)\s+score=([\d.]+)\s+conf=([\d.]+)\s+cf=([\d.]+)%",
    re.I,
)

_LEGACY_HF_BLOCK_MARKERS = (
    "only outputs json for trading policy",
    "switch to gguf (whatsapp_llm_provider=llama_cpp",
    "can't do conversational chat",
)

_OFFLINE_FALLBACK_MARKERS = (
    "copilot is in offline mode",
    "i can still read local state but not run the full llm",
)

_CONVERSATIONAL_EXAMPLES: list[dict[str, str]] = [
    {
        "role": "user",
        "content": "hey, how are we doing today?",
    },
    {
        "role": "assistant",
        "content": (
            "You're sitting around $3.18 equity with 9 shorts open — mostly small winners on paper "
            "but tight margin (about $0.97 free). The hourly optimizer loosened throughput because "
            "trades-per-hour is low, so the scanner is casting a wider net.\n\n"
            "Nothing alarming in the snapshot; want me to walk through a specific position or the "
            "latest PICK lines?"
        ),
    },
    {
        "role": "user",
        "content": "why is ETH still open if we're bearish?",
    },
    {
        "role": "assistant",
        "content": (
            "Good question. ETH is a **short** here, not a long — the engine opened it when confluence "
            "and markov regime aligned. It stays open until exchange SL/TP fires or the steward harvests "
            "at ~3R; we never flip-close on signal alone.\n\n"
            "Check the Positions tab for mark vs SL — if mark passed SL but the position is still open, "
            "the Blofin trigger may use last price, or the SL order may not have attached. Say *slcheck* "
            "style and I can explain what to verify."
        ),
    },
    {
        "role": "user",
        "content": "can you lower the pick floor a bit?",
    },
    {
        "role": "assistant",
        "content": (
            "I can patch that in code — the pick floor lives in `pick_engine.py` / optimizer overrides. "
            "Tell me how aggressive you want it (e.g. score 62 → 58) and I'll propose a minimal "
            "```edit path=pick_engine.py``` block. Live reload applies in ~3s; the engine keeps trading "
            "on its own schedule."
        ),
    },
]


def _read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _is_legacy_hf_blocker(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(m in low for m in _LEGACY_HF_BLOCK_MARKERS)


def _is_offline_fallback_reply(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(m in low for m in _OFFLINE_FALLBACK_MARKERS)


def prune_legacy_chat_history() -> int:
    """Remove poisoned copilot replies (HF blocker echo, offline snapshot dumps)."""
    if not CHAT_HISTORY.is_file():
        return 0
    try:
        lines = CHAT_HISTORY.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    kept: list[str] = []
    removed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        content = str(row.get("content") or "")
        if row.get("role") == "assistant" and (
            _is_legacy_hf_blocker(content) or _is_offline_fallback_reply(content)
        ):
            removed += 1
            continue
        kept.append(line)
    if removed:
        CHAT_HISTORY.write_text(
            ("\n".join(kept) + ("\n" if kept else "")),
            encoding="utf-8",
        )
        log.info("pruned %s legacy HF blocker messages from dashboard chat history", removed)
    return removed


def _load_history() -> deque[dict[str, str]]:
    out: deque[dict[str, str]] = deque(maxlen=MAX_HISTORY)
    if not CHAT_HISTORY.is_file():
        return out
    try:
        for line in CHAT_HISTORY.read_text(encoding="utf-8").splitlines()[-MAX_HISTORY * 2 :]:
            if not line.strip():
                continue
            row = json.loads(line)
            role, content = row.get("role"), row.get("content")
            if role in ("user", "assistant") and content:
                text = str(content)
                if role == "assistant" and (
                    _is_legacy_hf_blocker(text) or _is_offline_fallback_reply(text)
                ):
                    continue
                out.append({"role": role, "content": text})
    except Exception:
        pass
    return out


def get_history_for_ui() -> list[dict[str, str]]:
    """Last N turns for dashboard API."""
    return list(_load_history())[-UI_HISTORY_LIMIT:]


def _save_turn(role: str, content: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "content": content[:8000],
    }
    with CHAT_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _symbol_short(sym: str) -> str:
    return sym.split("/")[0] if "/" in sym else sym.replace(":USDT", "")


def _tail_log(n: int = 30) -> list[str]:
    if not LOG_FILE.is_file():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


def _top_picks_from_log(lines: list[str] | None = None, limit: int = 8) -> list[str]:
    """Parse recent PICK / CONFLUENCE lines from bot.log."""
    if lines is None:
        lines = _tail_log(2000)
    by_sym: dict[str, dict] = {}
    for line in lines:
        m_pick = _PICK_PAT.search(line)
        if m_pick:
            sym = m_pick.group(1)
            by_sym[sym] = {
                "symbol": _symbol_short(sym),
                "side": m_pick.group(2).lower(),
                "pick_score": float(m_pick.group(3)),
                "tier": m_pick.group(5),
                "status": "pick",
            }
            continue
        m_cf = _CONFLUENCE_PAT.search(line)
        if m_cf:
            sym = m_cf.group(1)
            row = by_sym.setdefault(sym, {"symbol": _symbol_short(sym)})
            row.update(
                {
                    "side": m_cf.group(2).lower(),
                    "confluence_pct": float(m_cf.group(5)),
                    "status": "confluence",
                }
            )
    rows = list(by_sym.values())
    rows.sort(
        key=lambda r: float(r.get("confluence_pct") or r.get("pick_score") or 0),
        reverse=True,
    )
    out: list[str] = []
    for r in rows[:limit]:
        side = r.get("side", "?")
        sym = r.get("symbol", "?")
        if r.get("status") == "confluence":
            out.append(
                f"  {sym} {side} confluence={r.get('confluence_pct', 0):.0f}% "
                f"(passed gate — engine may enter if slots free)"
            )
        else:
            out.append(
                f"  {sym} {side} pick_score={r.get('pick_score', 0):.2f} tier={r.get('tier', '?')}"
            )
    return out


def _summarize_positions(snap: dict, registry: dict) -> list[str]:
    lines: list[str] = []
    positions = snap.get("positions") or []
    if not positions and registry:
        for sym, reg in sorted(registry.items()):
            positions.append({"symbol": sym, **reg})

    for pos in positions[:12]:
        sym = _symbol_short(str(pos.get("symbol") or pos.get("position_key") or "?"))
        side = pos.get("side", "?")
        entry = float(pos.get("entry") or pos.get("entry_price") or 0)
        mark = float(pos.get("mark") or entry)
        sl = pos.get("sl_price")
        tp = pos.get("tp_price")
        pnl_pct = pos.get("pnl_pct")
        verified = pos.get("tpsl_verified_at")
        sl_hint = ""
        if sl:
            sl_hint = f"registry_sl={sl}"
            if side == "long" and mark <= float(sl):
                sl_hint += " (mark at/below SL — check exchange trigger)"
            elif side == "short" and mark >= float(sl):
                sl_hint += " (mark at/above SL — check exchange trigger)"
        elif verified:
            sl_hint = "tpsl_verified=yes"
        else:
            sl_hint = "no SL hint in registry — verify exchange SL"
        pnl_s = f" pnl={pnl_pct:+.1f}%" if pnl_pct is not None else ""
        lines.append(
            f"  {sym} {side} entry={entry:.6g} mark={mark:.6g}{pnl_s} {sl_hint} tp={tp}"
        )
    return lines


def _rich_dashboard_context() -> str:
    """Fast local context — no exchange REST unless FULL_CONTEXT env is set."""
    lines: list[str] = []
    now = time.time()

    snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
    if snap:
        age = max(0, now - float(snap.get("updated_at") or 0))
        lines.append(
            f"account: equity=${snap.get('equity')} free=${snap.get('free_margin')} "
            f"open={snap.get('open_count')} api_ok={snap.get('api_ok', True)} "
            f"snapshot_age_sec={age:.0f}"
        )
    else:
        lines.append("account: snapshot missing (bot may be offline)")

    registry = _read_json(STATE_DIR / "position_registry.json", {}) or {}
    pos_lines = _summarize_positions(snap, registry if isinstance(registry, dict) else {})
    if pos_lines:
        lines.append("open_positions:")
        lines.extend(pos_lines)
    elif snap.get("open_count"):
        lines.append(f"open_positions: {snap.get('open_count')} (details in snapshot only)")

    fluid = _read_json(STATE_DIR / "fluid_state.json", {}) or {}
    if fluid:
        lines.append(
            f"fluid: peak=${fluid.get('peak_equity')} trough=${fluid.get('trough_equity')} "
            f"phase={fluid.get('last_phase') or fluid.get('curve_phase')}"
        )

    markov = _read_json(STATE_DIR / "markov_regime.json", {}) or {}
    if markov:
        summary = markov.get("summary") or markov.get("global") or markov.get("state")
        lines.append(f"markov_regime: {summary or markov.get('ts', 'loaded')}")

    hourly = _read_json(STATE_DIR / "hourly_report.json", {}) or {}
    if hourly:
        tuning = hourly.get("tuning") or {}
        lines.append(
            f"hourly: equity={hourly.get('equity')} opens={hourly.get('open_count')} "
            f"optimizer={tuning.get('action')} tph={tuning.get('trades_last_hour')}"
        )

    prof = _read_json(STATE_DIR / "profitability.json", {}) or {}
    trades = prof.get("trades") or []
    if trades:
        recent = trades[-5:]
        wins = sum(1 for t in recent if float(t.get("net_pnl") or t.get("pnl_usd") or 0) > 0)
        lines.append(f"profitability: last_{len(recent)}_closes wins={wins}/{len(recent)}")

    stack_path = STATE_DIR / "stack_status.txt"
    if stack_path.is_file():
        stack_txt = stack_path.read_text(encoding="utf-8", errors="replace").strip()
        lines.append("stack_status:")
        lines.extend(f"  {ln}" for ln in stack_txt.splitlines()[:14])

    picks = _top_picks_from_log()
    if picks:
        lines.append("scanner_picks (from bot.log):")
        lines.extend(picks)

    log_tail = _tail_log(30)
    if log_tail:
        lines.append("bot.log tail:")
        lines.extend(f"  {ln}" for ln in log_tail)

    return "\n".join(lines)


def _full_context_with_timeout(timeout_sec: float = 8.0) -> str | None:
    result: list[str] = []
    err: list[str] = []

    def _worker() -> None:
        try:
            from whatsapp_agent import build_trading_context

            result.append(build_trading_context())
        except Exception as exc:
            err.append(str(exc))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return None
    if result:
        return result[0]
    if err:
        return f"exchange_context_error={err[0]}"
    return None


def _read_only_context() -> str:
    use_full = os.environ.get("DASHBOARD_COPILOT_FULL_CONTEXT", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    fast = _rich_dashboard_context()
    if not use_full:
        return fast
    full = _full_context_with_timeout(8.0)
    if full:
        return f"{fast}\n\n--- exchange REST (full) ---\n{full}"
    return fast + "\n(full exchange context timed out after 8s — using fast snapshot above)"


def _system_prompt(*, code_mode: bool) -> str:
    base = """You are God Bot Dashboard Copilot — a conversational trading engineer on the user's Windows machine.

Talk naturally: full paragraphs, mirror the user's tone, remember the thread. When something is ambiguous, ask **one** clarifying question before diving in. Explain *why* things happen (engine gates, Blofin exchange SL/TP, markov regime, pick/confluence scanner).

STYLE:
- 2–8 sentences for normal answers; longer when doing deep dives or multi-file code edits.
- Light markdown (bold, bullets) is fine.
- Never invent PnL or positions not in LIVE_SNAPSHOT.

TRADING (never break):
- You cannot open, close, resize, or route live orders. bot.py / core_brain own all entries and exits.
- Never say you placed a trade or changed leverage on an open position.
- For "open BTC now" style commands: refuse immediately and point to the automated engine.

CODE CHANGES (allowed when user asks):
- You MAY edit Python/config files to fix bugs or tune gates, scanners, dashboard, etc.
- After edits, live_update reloads .py modules in ~3s without full restart.
- Use ONLY this format for each file (one or more blocks):

```edit path=relative/path.py
<full new file content>
```

Paths must be under the repo (e.g. pick_engine.py, config.py, dashboard/src/App.tsx).
Do not edit state/, logs/, .git/, or delete secrets.
Keep edits minimal and correct.

ADVISORY:
- Explain equity, positions, SL/TP on exchange, scanner PICK/CONFLUENCE, logs, optimizer.
- Stops are EXCHANGE-MANAGED on Blofin — registry sl_price is the plan; the order must exist on exchange.
- Use LIVE_SNAPSHOT + TRAINED_CORTEX_KNOWLEDGE; cite what you see."""
    if code_mode:
        base += (
            "\nThe user wants a code change — prefer a concrete ```edit path=...``` block, "
            "then a brief summary of what changed and why."
        )
    return base


def _is_allowed_path(rel: str) -> bool:
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return False
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        return False
    if any(part in _BLOCKED_PARTS for part in target.parts):
        return False
    name = target.name
    if name in _ENV_NAMES:
        return True
    if target.suffix.lower() in _ALLOWED_SUFFIXES:
        return True
    return False


def _apply_edit_blocks(text: str) -> tuple[str, list[str]]:
    applied: list[str] = []
    notes: list[str] = []
    for match in _EDIT_BLOCK.finditer(text):
        rel = match.group(1).strip()
        body = match.group(2)
        if not body.endswith("\n"):
            body = body + "\n"
        if not _is_allowed_path(rel):
            notes.append(f"skipped {rel} (not allowed)")
            continue
        target = (ROOT / rel).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            applied.append(rel)
            with EDIT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "path": rel,
                            "bytes": len(body.encode("utf-8")),
                        }
                    )
                    + "\n"
                )
        except Exception as exc:
            notes.append(f"failed {rel}: {exc}")
    if not applied and not notes:
        return text, applied
    suffix = ""
    if applied:
        suffix += "\n\n[Copilot applied: " + ", ".join(applied) + ". Live reload ~3s.]"
    if notes:
        suffix += "\n[" + "; ".join(notes) + "]"
    return text + suffix, applied


def _wants_code_change(msg: str) -> bool:
    low = msg.lower()
    return bool(
        re.search(
            r"\b(fix|patch|change|update|edit|recode|refactor|implement|add|tweak|adjust|"
            r"modify|rewrite|code|\.py|dashboard|pick_engine|config|gate|threshold)\b",
            low,
        )
    )


def _reject_live_trade(msg: str) -> str | None:
    if _LIVE_TRADE_ORDER.match(msg.strip()):
        return (
            "I can't place live trades — God Bot (bot.py) handles all execution on its own schedule.\n\n"
            "If you're looking to get into BTC, the scanner needs a PICK → CONFLUENCE pass and a free slot. "
            "I can explain what the engine is seeing, or help tune pick gates in code if you want more throughput."
        )
    if re.search(r"\btrade\s+now\b|\bmarket\s+order\b", msg, re.I):
        return (
            "Live orders are engine-only — I won't route them from chat. "
            "Ask me about current scans, positions, or a code change to how entries work."
        )
    return None


def _chat_temperature(*, code_mode: bool) -> float:
    if code_mode:
        return 0.25
    return float(os.environ.get("DASHBOARD_COPILOT_TEMPERATURE", "0.58"))


def _wants_mission_plan(msg: str) -> bool:
    low = (msg or "").lower()
    return any(
        w in low
        for w in (
            "plan",
            "goal",
            "mission",
            "reach",
            "million",
            "target",
            "on track",
            "on-track",
            "schedule",
            "trajectory",
            "compound",
        )
    )


def _mission_plan_reply() -> str:
    """Instant mission answer from local optimizer (no LLM wait)."""
    from growth_optimizer import CompoundGrowthOptimizer, _day_start_equity
    from mission_config import TARGET_DAILY_GROWTH_PCT, progress_toward_daily_goal_pct, sole_objective_label

    snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
    eq = float(snap.get("equity") or 0)
    if eq <= 0:
        fluid = _read_json(STATE_DIR / "fluid_state.json", {}) or {}
        eq = float(fluid.get("peak_equity") or fluid.get("trough_equity") or 0)

    opt = CompoundGrowthOptimizer(state_dir=STATE_DIR)
    m = opt.get_growth_metrics(eq if eq > 0 else 2.94)
    day_start = _day_start_equity(opt.history, eq)
    today_pct = (eq / day_start - 1.0) * 100.0 if day_start > 0 and eq > 0 else 0.0
    progress_today = progress_toward_daily_goal_pct(today_pct)
    hourly = _read_json(STATE_DIR / "hourly_report.json", {}) or {}
    tuning = hourly.get("tuning") or {}
    api_ok = snap.get("api_ok", True)

    lines = [
        f"**Mission:** {sole_objective_label()}",
        f"**Now:** ${eq:.4f} equity · {int(snap.get('open_count') or 0)} opens · "
        f"today {today_pct:+.2f}% ({progress_today:.1f}% of +{TARGET_DAILY_GROWTH_PCT:.0f}% goal)",
        f"**Plan:** maintain/exceed **+{TARGET_DAILY_GROWTH_PCT:.0f}%** from day open "
        f"(${day_start:,.4f} → EOD target **${m.projected_capital_at_target:,.4f}**).",
        f"**Track:** {'on pace' if m.on_track else 'below +10%'} — "
        f"need **{m.required_daily_return_pct:.2f}%** today; aggression **{m.aggression_boost:.2f}x**.",
        "**How the bot executes it:**",
        "- 3R scalps, 50x where allowed, exchange SL/TP only, steward harvests winners.",
        "- Scanner + confluence + ML gates; cortex LLM policy when `LLM_TRADING_ENABLED=true`.",
        "- Hourly optimizer loosens/tightens throughput (recent: "
        f"{tuning.get('action', 'n/a')}, {tuning.get('notes', '')}).",
    ]
    if not api_ok:
        lines.append(
            "**Blocker:** Blofin REST is failing (`api_ok=False`, often passphrase 152408). "
            "Fix API keys in `.env` so live equity/positions refresh — bot still reads local snapshot."
        )
    lines.append(
        "\nAsk me to tune gates (code edit), explain a symbol, or run `slcheck` style SL questions."
    )
    return "\n".join(lines)[:MAX_REPLY_CHARS]


def _chat_max_tokens(*, code_mode: bool) -> int:
    if code_mode:
        return int(os.environ.get("DASHBOARD_COPILOT_CODE_MAX_TOKENS", "2400"))
    return int(os.environ.get("DASHBOARD_COPILOT_MAX_TOKENS", "720"))


def _call_llm(user_msg: str, *, code_mode: bool) -> str | None:
    from local_cortex import augmented_messages, train
    from local_llm import chat_completion, resolve_provider, status_line

    provider = resolve_provider()
    if provider == "none":
        return None

    ensure_copilot_llm_ready(block=True)
    if _LLM_STATE.get("status") != "ready":
        log.warning(
            "dashboard copilot LLM not ready (status=%s)",
            _LLM_STATE.get("status"),
        )
        return None

    hist = list(_load_history())
    snapshot = (
        f"UTC {datetime.now(timezone.utc).isoformat()}\n"
        f"llm={status_line()}\n"
        f"copilot_mode=advisory_and_code_edits_no_live_trades\n"
        f"{_read_only_context()}"
    )
    profile = "full" if code_mode else "chat"
    messages = augmented_messages(
        _system_prompt(code_mode=code_mode),
        snapshot,
        hist,
        user_msg,
        profile=profile,
    )

    shot_cap = int(os.environ.get("DASHBOARD_COPILOT_STYLE_EXAMPLES", "1"))
    insert_at = len(messages) - len(hist) - 1
    for i, ex in enumerate(_CONVERSATIONAL_EXAMPLES[: max(0, shot_cap)]):
        messages.insert(insert_at + i, ex)

    global _LAST_CORTEX_TRAIN
    now = time.time()
    if now - _LAST_CORTEX_TRAIN >= _CORTEX_TRAIN_INTERVAL:
        def _train_async() -> None:
            global _LAST_CORTEX_TRAIN
            try:
                train()
                _LAST_CORTEX_TRAIN = time.time()
            except Exception:
                log.debug("cortex refresh skipped", exc_info=True)

        threading.Thread(target=_train_async, daemon=True).start()

    timeout_sec = copilot_llm_timeout_sec()
    result: dict[str, Any] = {"text": None, "err": None}

    def _run_chat() -> None:
        result["text"], result["err"] = chat_completion(
            messages,
            max_tokens=_chat_max_tokens(code_mode=code_mode),
            temperature=_chat_temperature(code_mode=code_mode),
            timeout_sec=timeout_sec,
            mode="chat",
        )

    worker = threading.Thread(target=_run_chat, daemon=True)
    worker.start()
    worker.join(timeout=float(timeout_sec) + 15.0)
    if worker.is_alive():
        log.warning("dashboard copilot LLM timed out after %ss", timeout_sec)
        return None

    text, err = result["text"], result["err"]
    if text:
        return text[:MAX_REPLY_CHARS]
    if err and err != "no_local_llm":
        log.warning("dashboard copilot LLM error: %s", err)
    return None


def _smart_fallback(msg: str) -> str:
    low = msg.lower().strip()
    ctx = _read_only_context()
    picks = _top_picks_from_log()

    if _wants_code_change(msg):
        return (
            "I'd help patch that, but no chat-capable local LLM is running right now.\n\n"
            "Run: scripts\\setup_local_llm.ps1 (or keep WHATSAPP_LLM_PROVIDER=hf_local — "
            "it now supports chat and trading policy).\n\n"
            f"Current snapshot:\n{ctx[:1200]}"
        )

    if any(w in low for w in ("sl", "stop", "still open", "why open", "loss", "liquidat")):
        snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
        pos_lines = _summarize_positions(snap, _read_json(STATE_DIR / "position_registry.json", {}) or {})
        body = (
            "Stops on this bot are **exchange-managed** — Blofin SL/TP orders, not Python tick closes. "
            "If mark crossed SL but the position is still open, the trigger may use last price, or the "
            "SL order may never have attached.\n\n"
        )
        if pos_lines:
            body += "From registry / snapshot:\n" + "\n".join(pos_lines[:8])
        else:
            body += ctx[:900]
        return body[:MAX_REPLY_CHARS]

    if any(
        w in low
        for w in (
            "status",
            "how",
            "doing",
            "equity",
            "balance",
            "hey",
            "hello",
            "hi",
            "down",
            "red",
            "losing",
            "drawdown",
            "pnl",
            "bleeding",
        )
    ):
        snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
        eq = snap.get("equity")
        free = snap.get("free_margin")
        opens = snap.get("open_count", 0)
        intro = (
            f"Here's where things stand: about **${eq}** equity, **${free}** free margin, "
            f"**{opens}** open positions."
        )
        stack_path = STATE_DIR / "stack_status.txt"
        if stack_path.is_file():
            first_lines = stack_path.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
            intro += " " + " ".join(first_lines)
        if picks:
            intro += "\n\nRecent scanner activity:\n" + "\n".join(picks[:5])
        intro += "\n\nAsk about a symbol, SL check, or say what you'd like tuned in code."
        return intro[:MAX_REPLY_CHARS]

    if _wants_mission_plan(msg):
        return _mission_plan_reply()

    if any(w in low for w in ("scan", "pick", "confluence", "scanner")):
        if picks:
            return (
                "Latest picks from bot.log (engine owns entries — I only explain):\n\n"
                + "\n".join(picks)
                + "\n\nCONFLUENCE lines mean the symbol passed gates; PICK is pre-filter strength. "
                "Want detail on a specific symbol?"
            )
        return (
            "No recent PICK/CONFLUENCE lines in the log tail — the scanner may be between passes "
            "or the bot process is quiet. Check the Scanner tab or ask about overall status."
        )

    if any(w in low for w in ("position", "open", "trade", "portfolio")):
        snap = _read_json(STATE_DIR / "account_snapshot.json", {}) or {}
        pos_lines = _summarize_positions(snap, _read_json(STATE_DIR / "position_registry.json", {}) or {})
        if pos_lines:
            return (
                "Open book from local snapshot (not live REST):\n\n"
                + "\n".join(pos_lines)
                + "\n\nWhich symbol should I explain?"
            )

    from local_llm import resolve_provider, status_line

    prov = resolve_provider()
    hint = (
        f"LLM provider={prov} ({status_line()}). "
        if prov != "none"
        else "No LLM provider — set WHATSAPP_LLM_PROVIDER=hf_local or run LM Studio. "
    )
    warm = _LLM_STATE.get("status")
    warm_note = (
        " Copilot is still warming the local model — retry in a minute."
        if warm == "warming"
        else ""
    )
    return (
        f"Copilot could not finish an LLM reply in time ({hint}"
        f"timeout={copilot_llm_timeout_sec()}s).{warm_note}\n\n"
        "Try: **plan to reach goal**, status, scanner picks, SL questions, or a code tweak.\n"
        "Setup: `scripts\\apply_fast_llm_env.py` then restart dashboard.\n\n"
        f"{ctx[:900]}"
    )


def reply_to_message(user_msg: str) -> str:
    user_msg = (user_msg or "").strip()
    if not user_msg:
        return "Ask about the bot, scans, or request a code change — I do not place trades."

    blocked = _reject_live_trade(user_msg)
    if blocked:
        return blocked

    code_mode = _wants_code_change(user_msg)
    if _wants_mission_plan(user_msg) and not code_mode:
        # Instant answer from growth optimizer — never block chat on HF load.
        text = _mission_plan_reply()
    else:
        text = _call_llm(user_msg, code_mode=code_mode)
    if text and _is_legacy_hf_blocker(text):
        log.warning("copilot suppressed legacy HF blocker echo")
        text = None
    if text is None:
        text = _smart_fallback(user_msg)
    else:
        text, _ = _apply_edit_blocks(text)

    _save_turn("user", user_msg)
    _save_turn("assistant", text)
    return text
