#!/usr/bin/env python3
"""
Tail bot.log + live state — auto-pause entries, nudge optimizer, trigger ML heal hints.

Run from stack_guard.ps1 every ~5 minutes (no second bot process).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402
from runtime_gates import (  # noqa: E402
    clear_entries_pause,
    read_entries_pause,
    set_entries_pause,
)

LOG = ROOT / "logs" / "bot.log"
ACTIONS = ROOT / "state" / "log_watch_actions.jsonl"
STATUS = ROOT / "state" / "log_watch.json"

EQUITY_RE = re.compile(
    r"equity=\$([\d.]+).*?open=(\d+).*?dd=([\d.]+)%\s*\|\s*(?:account_curve|pnl)=(\w+).*?vert=([\d.]+)%"
)
SL_RE = re.compile(r"outcome .+ loss")
TPSL_WARN_RE = re.compile(r"steward: (\d+)/(\d+) positions missing exchange TP/SL")
ML_MISMATCH_RE = re.compile(r"ML feature count (\d+) != code (\d+)")


def _tail_lines(path: Path, n: int = 400) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return data[-n:]
    except OSError:
        return []


def _append_action(row: dict) -> None:
    ACTIONS.parent.mkdir(parents=True, exist_ok=True)
    with ACTIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def analyze() -> dict:
    settings = load_settings()
    state_dir = settings.state_dir
    lines = _tail_lines(LOG, 500)
    now = time.time()
    actions: list[str] = []
    anomalies: list[str] = []

    live_eq = 0.0
    snap_path = state_dir / "account_snapshot.json"
    if snap_path.is_file():
        try:
            live_eq = float(json.loads(snap_path.read_text(encoding="utf-8")).get("equity") or 0)
        except Exception:
            pass

    last_eq = live_eq
    last_open = 0
    last_phase = ""
    last_dd = 0.0
    sl_count = 0
    tpsl_missing = 0
    ml_mismatch = False

    for line in lines:
        m = EQUITY_RE.search(line)
        if m:
            last_eq = float(m.group(1))
            last_open = int(m.group(2))
            last_dd = float(m.group(3))
            last_phase = m.group(4)
        if SL_RE.search(line):
            sl_count += 1
        tw = TPSL_WARN_RE.search(line)
        if tw:
            tpsl_missing = max(tpsl_missing, int(tw.group(1)))
        if ML_MISMATCH_RE.search(line):
            ml_mismatch = True

    paused, pause_reason = read_entries_pause(state_dir)

    micro_cap = getattr(settings, "micro_equity_threshold", 10.0) * 2.5

    # Pause new entries: micro accounts only — declining book + full slots
    if (
        last_eq > 0
        and last_eq < micro_cap
        and last_phase == "declining"
        and last_open >= 5
        and last_dd >= 7.0
        and not paused
    ):
        set_entries_pause(
            state_dir,
            900.0,
            f"declining curve dd={last_dd:.1f}% with {last_open} opens — 15m entry pause",
        )
        actions.append("pause_entries_15m_declining_full_book")
        paused = True

    # Burst stop-outs (micro only — funded accounts keep trading)
    if sl_count >= 4 and last_eq > 0 and last_eq < micro_cap and not paused:
        set_entries_pause(
            state_dir,
            600.0,
            f"{sl_count} SL outcomes in log tail — 10m cooldown on new entries",
        )
        actions.append("pause_entries_10m_sl_burst")
        paused = True
        anomalies.append(f"sl_burst={sl_count}")

    # Clear pause when curve climbing and book light
    if paused and last_phase in ("climbing", "vertical") and last_open <= 2 and last_dd < 4.0:
        clear_entries_pause(state_dir)
        actions.append("clear_pause_curve_recovered")
        paused = False

    # ML schema drift hint for self_heal / trainer (state flag)
    heal_flag = state_dir / "ml_force_refit.flag"
    if ml_mismatch:
        heal_flag.write_text(json.dumps({"ts": now, "reason": "feature_mismatch"}), encoding="utf-8")
        actions.append("ml_force_refit_flag")
        anomalies.append("ml_feature_mismatch")

    # Optimizer tick when enabled and bot running
    opt_note = ""
    if settings.optimizer_enabled and last_eq > 0:
        try:
            from scalp_optimizer import ScalpOptimizer
            from exchange_client import BlofinExchange

            ex = BlofinExchange(settings)
            ex.load()
            rep = ScalpOptimizer(state_dir, settings).maybe_optimize(
                ex.fetch_equity_usdt(), force=False
            )
            if rep:
                opt_note = rep.summary
                actions.append(f"optimizer:{rep.action}")
        except Exception as e:
            opt_note = f"skip:{e}"

    report = {
        "ts": now,
        "equity": last_eq,
        "open": last_open,
        "curve_phase": last_phase,
        "dd_pct": last_dd,
        "entries_paused": paused,
        "pause_reason": pause_reason if paused else "",
        "tpsl_missing_max": tpsl_missing,
        "sl_in_tail": sl_count,
        "actions": actions,
        "anomalies": anomalies,
        "optimizer": opt_note,
    }
    STATUS.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if actions:
        _append_action(report)
    return report


def main() -> None:
    rep = analyze()
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
