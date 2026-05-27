#!/usr/bin/env python3
"""Runs autonomous money-adjacent tasks with zero user input (existing .env only)."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "state" / "hustle_report.jsonl"
PID_FILE = ROOT / "state" / "bot.pid"


def _setup_log() -> None:
    path = ROOT / "logs" / "hustle_daemon.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _append_report(row: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    row["ts"] = time.time()
    with REPORT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _bot_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    try:
        subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        return str(pid) in out and "python" in out.lower()
    except Exception:
        return False


def _ensure_bot() -> None:
    if _bot_running():
        logging.info("bot already running pid=%s", PID_FILE.read_text(encoding="utf-8").strip())
        return
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    log_path = ROOT / "logs" / "bot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(py), str(ROOT / "bot.py")],
        cwd=str(ROOT),
        stdout=open(log_path, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    logging.info("started bot.py pid=%s", proc.pid)


def _treasury_cycle() -> dict:
    try:
        from treasury.consolidator import run_cycle
        from treasury.settings import load_treasury_settings

        return run_cycle(load_treasury_settings())
    except FileNotFoundError as e:
        logging.warning("treasury skipped: %s", e)
        return {"skipped": True, "reason": str(e)}
    except Exception:
        logging.exception("treasury cycle failed")
        return {"error": True}


def _equity_snapshot() -> dict:
    try:
        from config import load_settings
        from blofin_http import BlofinHttp

        s = load_settings()
        h = BlofinHttp(s.api_key, s.secret, s.passphrase, demo=s.mode == "demo")
        bal = h.get_balance("futures")
        if isinstance(bal, dict):
            eq = float(bal.get("totalEquity") or 0)
            pos = h.get_positions()
            n_pos = len(pos) if isinstance(pos, list) else 0
            return {"equity": eq, "open_positions": n_pos, "mode": s.mode, "dry_run": s.dry_run}
    except Exception:
        logging.exception("equity snapshot failed")
    return {}


def run_once(ensure_bot: bool) -> None:
    if ensure_bot:
        _ensure_bot()

    snap = _equity_snapshot()
    if snap:
        logging.info(
            "blofin equity=$%.2f positions=%s dry_run=%s",
            snap.get("equity", 0),
            snap.get("open_positions", 0),
            snap.get("dry_run"),
        )

    treasury = _treasury_cycle()
    row = {"snap": snap, "treasury": treasury}
    _append_report(row)
    logging.info("cycle done %s", row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-bot", action="store_true", help="Do not start/restart bot.py")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    _setup_log()
    ensure_bot = not args.no_bot

    if args.once:
        run_once(ensure_bot)
        return

    logging.info("hustle daemon loop interval=%ds ensure_bot=%s", args.interval, ensure_bot)
    while True:
        try:
            run_once(ensure_bot)
        except Exception:
            logging.exception("loop error")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
