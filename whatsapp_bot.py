#!/usr/bin/env python3
"""
WhatsApp Bot for Blofin AI Trader — Hermes-style interface.
Text your bot and get real-time trading status, positions, PnL, and control.

Setup:
  1. Create a Twilio account (free trial: https://twilio.com)
  2. Get a WhatsApp-enabled number or use the Sandbox
  3. Install ngrok: https://ngrok.com/download
  4. Run: ngrok http 5000
  5. Set Twilio webhook to: https://YOUR_NGROK.ngrok.io/whatsapp
  6. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in blofin-auto-trader/.env

Commands you can text:
  - "status" or "s" — bot status, equity, open positions
  - "positions" or "p" — list all open positions with PnL
  - "balance" or "b" — account equity and free margin
  - "trades" or "t" — recent trade history
  - "stats" — win rate, profit factor, total PnL
  - "signals" — latest high-confidence signals
  - "help" or "h" — show all commands
  - "start" — restart the trading bot (if stopped)
  - "stop" — pause new trades
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

# Load .env file for Twilio credentials
from dotenv import load_dotenv
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

from config import load_settings
from exchange_client import BlofinExchange
from markets import compute_max_open_positions
log = logging.getLogger(__name__)

# ─── State ──────────────────────────────────────────────────────────────
STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
BOT_PID_FILE = STATE_DIR / "bot.pid"
BOT_STATUS_FILE = STATE_DIR / "bot_status.json"

# ─── Flask App ──────────────────────────────────────────────────────────
app = Flask(__name__)

# ─── Twilio Client ─────────────────────────────────────────────────────
_twilio_client: TwilioClient | None = None
_MY_NUMBER = None  # Your WhatsApp number for notifications


def get_twilio() -> TwilioClient | None:
    global _twilio_client
    if _twilio_client is None:
        sid = os.environ.get("TWILIO_ACCOUNT_SID") or ""
        token = os.environ.get("TWILIO_AUTH_TOKEN") or ""
        if sid and token:
            _twilio_client = TwilioClient(sid, token)
    return _twilio_client


def send_whatsapp(to_number: str, message: str) -> bool:
    """Send a WhatsApp message via Twilio."""
    client = get_twilio()
    if not client:
        log.warning("Twilio not configured")
        return False
    from_num = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    try:
        client.messages.create(
            body=message,
            from_=from_num,
            to=f"whatsapp:{to_number}",
        )
        return True
    except Exception as e:
        log.error("Failed to send WhatsApp: %s", e)
        return False


# ─── Bot State ──────────────────────────────────────────────────────────

def is_bot_running() -> bool:
    from whatsapp_agent import is_bot_running as _agent_running

    return _agent_running()


def _stack_control(action: str) -> str:
    ps1 = Path(__file__).parent / "scripts" / "stack_control.ps1"
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Action", action],
            text=True,
            timeout=45,
            cwd=str(Path(__file__).parent),
            stderr=subprocess.STDOUT,
        )
        return (out or "").strip() or f"stack {action} done"
    except subprocess.CalledProcessError as e:
        return f"stack {action} failed: {(e.output or e)!s}"[:500]
    except Exception as e:
        return f"stack {action} error: {e}"


def read_trade_log(n: int = 10) -> list[dict]:
    log_file = STATE_DIR / "trades.jsonl"
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    trades = []
    for line in lines[-n:]:
        if line.strip():
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return trades


def get_latest_log_lines(n: int = 15) -> str:
    log_file = Path(__file__).parent / "logs" / "bot.log"
    if not log_file.exists():
        return "No bot log found."
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    return "\n".join(lines[-n:])


# ─── Command Handlers ──────────────────────────────────────────────────

def cmd_status() -> str:
    """Full bot status — equity, positions, leverage summary."""
    try:
        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()

        equity = ex.fetch_equity_usdt()
        free = ex.fetch_free_equity_usdt()
        positions = ex.fetch_all_positions()

        from universe import load_tradeable_markets
        markets = load_tradeable_markets(ex, equity, settings.leverage, settings.margin_utilization, settings.max_positions)
        max_open = compute_max_open_positions(equity, len(markets),
            cap=settings.max_positions, auto_balance=settings.auto_max_positions,
            margin_utilization=settings.margin_utilization,
            markets=[ex.market_for(m.symbol) for m in markets if ex.market_for(m.symbol)],
            leverage=settings.leverage)

        running = is_bot_running()
        lines = [
            f"🤖 *Blofin AI Trader*",
            f"┌ Status: {'🟢 RUNNING' if running else '🔴 STOPPED'}",
            f"├ Equity: ${equity:.4f}",
            f"├ Free: ${free:.4f}",
            f"├ Open Positions: {len(positions)}",
            f"├ Max Positions: {max_open}",
            f"├ Universe: {len(markets)} markets",
            f"├ Max Leverage: {settings.auto_leverage_max}x",
            f"└ Last Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        ]

        if positions:
            lines.append("")
            lines.append("*Open Positions:*")
            for sym, pos in list(positions.items())[:5]:
                side = pos.get("side", "?")
                contracts = pos.get("contracts", 0)
                lines.append(f"  • {sym} {side.upper()} {contracts:.2f} ct")

        trades = read_trade_log(5)
        if trades:
            lines.append("")
            lines.append("*Recent:*")
            for t in trades[-3:]:
                sym = t.get("symbol", "?")
                pnl = t.get("pnl_usd", 0)
                arrow = "📈" if pnl >= 0 else "📉"
                lines.append(f"  {arrow} {sym} ${pnl:.4f}")

        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Error fetching status: {e}"


def cmd_positions() -> str:
    """Detailed open position list with PnL."""
    try:
        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        positions = ex.fetch_all_positions()

        if not positions:
            return "📭 No open positions."

        lines = ["📊 *Open Positions:*", ""]
        for sym, pos in sorted(positions.items()):
            side = pos.get("side", "long")
            contracts = pos.get("contracts", 0)
            market = ex.market_for(sym)
            entry = pos.get("entry_price", 0)
            info = pos.get("info", {})
            mark = float(info.get("markPx") or info.get("markPrice") or 0)
            if entry > 0 and mark > 0:
                if side == "long":
                    pnl_pct = (mark / entry - 1) * 100
                else:
                    pnl_pct = (entry / mark - 1) * 100
            else:
                pnl_pct = 0

            icon = "🟢" if pnl_pct >= 0 else "🔴"
            lev = info.get("leverage") or info.get("lever") or "?"
            lines.append(f"{icon} *{sym}*")
            lines.append(f"   Side: {side.upper()}  Size: {contracts:.4f}")
            lines.append(f"   Entry: ${entry:.6f}  Mark: ${mark:.6f}")
            lines.append(f"   PnL: {pnl_pct:+.2f}%  Leverage: {lev}x")
            lines.append("")

        return "\n".join(lines).strip()
    except Exception as e:
        return f"⚠️ Error: {e}"


def cmd_balance() -> str:
    """Account balance details."""
    try:
        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        equity = ex.fetch_equity_usdt()
        free = ex.fetch_free_equity_usdt()
        positions = ex.fetch_all_positions()
        used = equity - free
        return (
            f"💰 *Balance Summary*\n"
            f"Total Equity: ${equity:.4f}\n"
            f"Free Margin: ${free:.4f}\n"
            f"Used Margin: ${used:.4f}\n"
            f"Open Positions: {len(positions)}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    except Exception as e:
        return f"⚠️ Error: {e}"


def cmd_trades() -> str:
    """Recent trade history with PnL."""
    trades = read_trade_log(20)
    if not trades:
        return "📭 No recent trades."

    lines = ["📜 *Recent Trades*", ""]
    wins = 0
    total_pnl = 0.0
    for t in trades[-10:]:
        sym = t.get("symbol", "?")
        side = t.get("side", "long")
        pnl = t.get("pnl_usd") or t.get("net_pnl") or 0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        icon = "✅" if pnl >= 0 else "❌"
        entry = t.get("entry_price", 0)
        exit_px = t.get("exit_price", 0)
        leverage = t.get("leverage", "?")
        lines.append(f"{icon} {sym} {side.upper()}")
        lines.append(f"   Entry: ${entry:.6f} → Exit: ${exit_px:.6f}")
        lines.append(f"   PnL: ${pnl:.4f}  Lev: {leverage}x")
        lines.append("")

    win_rate = (wins / max(len(trades[-10:]), 1)) * 100
    lines.append(f"📊 Win Rate: {win_rate:.0f}%  Total PnL: ${total_pnl:.4f}")
    return "\n".join(lines).strip()


def cmd_stats() -> str:
    """Trading statistics."""
    trades = read_trade_log(100)
    if not trades:
        return "📊 Not enough trade data yet."

    total = len(trades)
    wins = sum(1 for t in trades if (t.get("pnl_usd") or t.get("net_pnl") or 0) > 0)
    losses = total - wins
    win_rate = (wins / total) * 100 if total > 0 else 0

    gross_profit = sum(t.get("pnl_usd") or t.get("net_pnl") or 0 for t in trades if t.get("pnl_usd", 0) > 0)
    gross_loss = abs(sum(t.get("pnl_usd") or t.get("net_pnl") or 0 for t in trades if t.get("pnl_usd", 0) < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win = gross_profit / wins if wins > 0 else 0
    avg_loss = gross_loss / losses if losses > 0 else 0

    return (
        f"📊 *Trading Stats*\n"
        f"Total Trades: {total}\n"
        f"Wins: {wins}  Losses: {losses}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Profit Factor: {pf:.2f}\n"
        f"Avg Win: ${avg_win:.4f}\n"
        f"Avg Loss: ${avg_loss:.4f}\n"
        f"Gross PnL: ${gross_profit - gross_loss:.4f}"
    )


def cmd_signals() -> str:
    """Latest high-confidence signals from the log."""
    log_text = get_latest_log_lines(30)
    signal_lines = []
    for line in log_text.split("\n"):
        if "ML SIGNAL" in line and "score=" in line:
            signal_lines.append(line.strip())

    if not signal_lines:
        return "📡 No recent signals. Bot may not be running."

    lines = ["📡 *Latest Signals*", ""]
    for s in signal_lines[-8:]:
        try:
            parts = s.split(" INFO ")[1] if " INFO " in s else s
            # Parse out key info
            sym_part = parts.split(" long ")[0] if " long " in parts else parts
            rest = parts.split(" long ")[1] if " long " in parts else ""
            score = ""
            lev = ""
            if "score=" in rest:
                score = rest.split("score=")[1].split()[0]
            if "lev=" in rest:
                lev = rest.split("lev=")[1].split("x")[0] + "x"
            symbol = sym_part.replace("ML SIGNAL ", "")
            lines.append(f"  🔥 {symbol}")
            lines.append(f"     Score: {score}  Leverage: {lev}")
        except Exception:
            lines.append(f"  {s[:80]}")

    return "\n".join(lines)


def cmd_help() -> str:
    return (
        "🤖 *Blofin AI Trader Commands*\n\n"
        "• *status* or *s* — Bot status, equity, positions\n"
        "• *positions* or *p* — Open positions with PnL\n"
        "• *balance* or *b* — Account equity & margin\n"
        "• *trades* or *t* — Recent trade history\n"
        "• *stats* — Win rate, profit factor\n"
        "• *signals* — Latest ML signals\n"
        "• *log* — Last 15 bot log lines\n"
        "• *help* or *h* — Show this menu\n"
        "• *slcheck* — Exchange SL vs mark per position\n"
        "• *restart* — Restart bot via stack_control\n"
        "• *start* — Start trading bot\n"
        "• *stop* — Stop the trading bot\n\n"
        "Or ask anything in plain English (local LLM if configured)."
    )


# ─── Webhook Handler ─────────────────────────────────────────────────

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    incoming_raw = request.values.get("Body", "").strip()
    incoming_msg = incoming_raw.lower()
    from_number = request.values.get("From", "").replace("whatsapp:", "")

    log.info("WhatsApp from %s: %s", from_number, incoming_msg)

    # Route commands
    if incoming_msg in ("status", "s"):
        response = cmd_status()
    elif incoming_msg in ("positions", "p"):
        response = cmd_positions()
    elif incoming_msg in ("balance", "b"):
        response = cmd_balance()
    elif incoming_msg in ("trades", "t"):
        response = cmd_trades()
    elif incoming_msg == "stats":
        response = cmd_stats()
    elif incoming_msg in ("signals", "sig"):
        response = cmd_signals()
    elif incoming_msg in ("help", "h"):
        response = cmd_help()
    elif incoming_msg in ("slcheck", "sl"):
        from whatsapp_agent import cmd_slcheck

        response = cmd_slcheck()
    elif incoming_msg == "log":
        log_text = get_latest_log_lines(15)
        response = f"📋 *Recent Log*\n\n{log_text[:1500]}"
    elif incoming_msg == "start":
        if is_bot_running():
            response = "🤖 Bot is already running."
        else:
            response = "✅ " + _stack_control("start")
    elif incoming_msg in ("stop",):
        if not is_bot_running():
            response = "🤖 Bot is not running."
        else:
            response = "🛑 " + _stack_control("stop")
    elif incoming_msg == "restart":
        response = "🔄 " + _stack_control("restart")
    else:
        from whatsapp_agent import reply_to_message

        response = reply_to_message(from_number, incoming_raw)

    # Build TwiML response
    twiml = MessagingResponse()
    twiml.message(response)
    return str(twiml)


@app.route("/health", methods=["GET"])
def health():
    return "WhatsApp Bot is running.", 200


# ─── Entry Point ───────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(Path(__file__).parent / "logs" / "whatsapp.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log.info("WhatsApp Bot starting on port 5000")
    log.info("Set Twilio webhook to: https://YOUR_NGROK_URL.ngrok.io/whatsapp")
    log.info("Commands: status, positions, balance, trades, stats, signals, help, start, stop")

    (Path(__file__).parent / "logs").mkdir(parents=True, exist_ok=True)

    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
