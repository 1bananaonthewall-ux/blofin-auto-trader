"""
Conversational WhatsApp agent for blofin-auto-trader.

Local LLM (no Ollama): llama-cpp-python + GGUF, or LM Studio / llama.cpp server.
Falls back to keyword routing when no local model is available.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CHAT_HISTORY = STATE_DIR / "whatsapp_chat_history.jsonl"
MAX_HISTORY = 12
MAX_REPLY_CHARS = 1500
_LAST_CORTEX_TRAIN = 0.0
_CORTEX_TRAIN_INTERVAL = 300.0


def _allowed_senders() -> set[str]:
    raw = os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").strip()
    if not raw:
        return set()
    return {n.strip().lstrip("+") for n in raw.split(",") if n.strip()}


def is_sender_allowed(phone: str) -> bool:
    allowed = _allowed_senders()
    if not allowed:
        return True
    clean = phone.replace("whatsapp:", "").lstrip("+")
    return clean in allowed or phone in allowed


def is_bot_running() -> bool:
    try:
        ps1 = ROOT / "scripts" / "stack_control.ps1"
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
                "-Action",
                "status",
            ],
            text=True,
            timeout=20,
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
        )
        return "bot.py: RUNNING" in out or "WARMUP" in out
    except Exception:
        return False


def _pending_tpsl(ex, symbol: str) -> list[dict]:
    from markets import symbol_to_inst_id

    inst = symbol_to_inst_id(symbol)
    return ex._safe_request(ex.http.get_pending_tpsl, inst) or []


def build_trading_context() -> str:
    """Snapshot for the LLM — equity, positions, SL on exchange, brain state."""
    lines: list[str] = []
    try:
        sys.path.insert(0, str(ROOT))
        from config import load_settings
        from exchange_client import BlofinExchange
        from markov_regime import get_markov_engine

        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        equity = ex.fetch_equity_usdt()
        free = ex.fetch_free_equity_usdt()
        positions = ex.fetch_all_positions()
        mk = get_markov_engine(settings.state_dir).last("global")

        lines.append(f"mode={settings.mode} dry_run={settings.dry_run} live={settings.mode=='live'}")
        lines.append(f"bot_process_running={is_bot_running()}")
        lines.append(f"equity_usd={equity:.4f} free_margin_usd={free:.4f} open_count={len(positions)}")
        if mk:
            lines.append(f"markov_global={mk.summary}")

        hr = STATE_DIR / "hourly_report.json"
        if hr.is_file():
            try:
                raw = json.loads(hr.read_text(encoding="utf-8"))
                lines.append(
                    f"hourly: equity={raw.get('equity')} opens={raw.get('open_count')} "
                    f"optimizer={raw.get('tuning', {}).get('action')} tph={raw.get('tuning', {}).get('trades_last_hour')}"
                )
            except Exception:
                pass

        for sym, pos in sorted(positions.items()):
            side = pos.get("side", "?")
            entry = float(pos.get("entry_price") or 0)
            mark = float(pos.get("mark_price") or entry)
            liq = float(pos.get("liquidation_price") or 0)
            pending = _pending_tpsl(ex, sym)
            sl = tp = None
            if pending:
                sl = float(pending[0].get("slTriggerPrice") or 0) or None
                tp = float(pending[0].get("tpTriggerPrice") or 0) or None
            beyond_sl = ""
            if sl and side == "long" and mark <= sl:
                beyond_sl = "mark_at_or_below_exchange_SL"
            elif sl and side == "short" and mark >= sl:
                beyond_sl = "mark_at_or_above_exchange_SL"
            elif not pending:
                beyond_sl = "NO_EXCHANGE_SL_ORDER"
            sym_short = sym.split("/")[0]
            lines.append(
                f"position {sym_short} {side} entry={entry:.6f} mark={mark:.6f} "
                f"sl={sl} tp={tp} liq={liq:.6f} exchange_sltp={'yes' if pending else 'NO'} {beyond_sl}"
            )

        log_tail = ROOT / "logs" / "bot.log"
        if log_tail.is_file():
            tail = log_tail.read_text(encoding="utf-8", errors="replace").strip().split("\n")[-8:]
            lines.append("recent_log:")
            lines.extend(f"  {ln}" for ln in tail)
    except Exception as exc:
        lines.append(f"context_error={exc}")
    return "\n".join(lines)


def cmd_slcheck() -> str:
    """Human-readable SL audit for WhatsApp."""
    try:
        sys.path.insert(0, str(ROOT))
        from config import load_settings
        from exchange_client import BlofinExchange

        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        positions = ex.fetch_all_positions()
        if not positions:
            return "No open positions."

        lines = ["SL check (exchange orders):", ""]
        for sym, pos in sorted(positions.items()):
            side = pos.get("side", "?")
            mark = float(pos.get("mark_price") or pos.get("entry_price") or 0)
            pending = _pending_tpsl(ex, sym)
            sym_short = sym.split("/")[0]
            if not pending:
                lines.append(f"*{sym_short}* {side}: NO SL on exchange")
                continue
            sl = float(pending[0].get("slTriggerPrice") or 0) or None
            tp = float(pending[0].get("tpTriggerPrice") or 0) or None
            warn = ""
            if sl and side == "long" and mark <= sl:
                warn = " (mark at/below SL — may not have triggered)"
            elif sl and side == "short" and mark >= sl:
                warn = " (mark at/above SL — may not have triggered)"
            lines.append(f"*{sym_short}* {side}: SL={sl} TP={tp}{warn}")
        return "\n".join(lines)[:MAX_REPLY_CHARS]
    except Exception as exc:
        return f"SL check failed: {exc}"


def _load_history(phone: str) -> deque[dict[str, str]]:
    hist: deque[dict[str, str]] = deque(maxlen=MAX_HISTORY)
    if not CHAT_HISTORY.is_file():
        return hist
    try:
        for line in CHAT_HISTORY.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("phone") != phone:
                continue
            hist.append({"role": row["role"], "content": row["content"]})
    except Exception:
        pass
    return hist


def _save_turn(phone: str, role: str, content: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phone": phone,
        "role": role,
        "content": content[:4000],
    }
    with CHAT_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _system_prompt() -> str:
    return """You are the Blofin 3R scalper's WhatsApp co-pilot. You speak clearly and honestly like a trading engineer.

You have LIVE context below from the user's machine (equity, positions, whether exchange SL orders exist).

Critical facts about this bot:
- Stops are EXCHANGE-MANAGED (Blofin TP/SL orders), not closed by Python on every tick.
- If exchange_sltp=NO, there is NO stop on the exchange — the bot registry may still show a planned stop_pct but nothing will auto-close.
- If mark passed the SL trigger but position is open: SL may use "last" price not mark, order failed to attach, or SL was never placed.
- The bot does NOT close on signal flip (never_close_on_signal_flip). Steward harvests winners; losses rely on SL or manual close.
- Mission: 50x where allowed, 3R R:R, core_brain + markov regime filter.

You are trained on this user's live trade_outcomes and playbooks (TRAINED_CORTEX_KNOWLEDGE).
Prefer cortex stats over generic finance advice. Under 1200 chars. Bullets OK.
Commands: restart, stop, status, positions, slcheck.
Never invent positions or PnL not in LIVE_SNAPSHOT. If unsure, say what to check."""


def _call_llm(phone: str, user_msg: str) -> str | None:
    from local_cortex import augmented_messages, train
    from local_llm import chat_completion, resolve_provider, status_line

    if resolve_provider() == "none":
        return None

    global _LAST_CORTEX_TRAIN
    now = datetime.now(timezone.utc).timestamp()
    if now - _LAST_CORTEX_TRAIN >= _CORTEX_TRAIN_INTERVAL:
        try:
            train()
            _LAST_CORTEX_TRAIN = now
        except Exception:
            log.debug("cortex refresh skipped", exc_info=True)

    hist = list(_load_history(phone))
    snapshot = (
        f"UTC {datetime.now(timezone.utc).isoformat()}\n"
        f"llm={status_line()}\n{build_trading_context()}"
    )
    messages = augmented_messages(
        _system_prompt(), snapshot, hist, user_msg, profile="chat"
    )

    text, err = chat_completion(messages, max_tokens=900, temperature=0.22, mode="chat")
    if text:
        return text[:MAX_REPLY_CHARS]
    if err and err != "no_local_llm":
        return f"Local AI error ({err}). Try: status, slcheck, help"
    return None


def _fallback_reply(msg: str) -> str:
    low = msg.lower().strip()
    if any(w in low for w in ("stop", "sl", "still open", "why open", "loss")):
        return cmd_slcheck() if "slcheck" in low else (
            "Text *slcheck* for each position's exchange SL.\n\n"
            "No SL on exchange = repair failed. Mark past SL but open = last-price trigger or no order."
        )
    if any(w in low for w in ("status", "how", "doing", "equity", "balance")):
        from whatsapp_bot import cmd_status

        return cmd_status()
    if any(w in low for w in ("position", "open", "trade")):
        from whatsapp_bot import cmd_positions

        return cmd_positions()
    return (
        "Local AI not ready.\n\n"
        "Set WHATSAPP_LLM_PROVIDER=auto (or hf_local / llama_cpp) in .env, then run "
        "scripts\\setup_local_llm.ps1 if needed.\n\n"
        "Commands: status, positions, slcheck, restart, help"
    )


def reply_to_message(phone: str, user_msg: str) -> str:
    if not is_sender_allowed(phone):
        return "This number is not authorized for bot control."

    user_msg = user_msg.strip()
    if not user_msg:
        return "Send me a question or try *help*."

    text = _call_llm(phone, user_msg)
    if text is None:
        text = _fallback_reply(user_msg)

    _save_turn(phone, "user", user_msg)
    _save_turn(phone, "assistant", text)
    return text
