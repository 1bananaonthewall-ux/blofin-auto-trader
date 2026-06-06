#!/usr/bin/env python3
"""
Five-minute God Bot maintain pass — no second bot, no LLM.

Runs from Task Scheduler (BlofinCursorAgent5m) and from the blofin-5m Cursor skill.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPORT = ROOT / "state" / "agent_5m_report.json"
FLAG = ROOT / ".cursor" / "AGENT_5M_DUE"
LOG = ROOT / "logs" / "cursor_agent_5m.log"


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stack_ensure() -> str:
    ps1 = ROOT / "scripts" / "stack_control.ps1"
    try:
        out = subprocess.check_output(
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
            text=True,
            timeout=90,
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
        )
        line = next(
            (ln.strip() for ln in (out or "").splitlines() if "bot.py" in ln.lower()),
            "ensure ok",
        )
        return line[:200]
    except Exception as exc:
        return f"ensure_fail:{exc}"[:200]


def _touch_agent_flag(now: float, anomalies: list[str]) -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    body = f"due_since={int(now)}\ninterval_sec=300\nanomalies={len(anomalies)}\n"
    FLAG.write_text(body, encoding="utf-8")


def main() -> int:
    now = time.time()
    from config import load_settings

    settings = load_settings()
    state_dir = settings.state_dir

    stack_note = _stack_ensure()

    log_watch: dict = {}
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from log_watch_optimizer import analyze

        log_watch = analyze()
    except Exception as exc:
        log_watch = {"error": str(exc)}

    pnl = _read_json(state_dir / "pnl_curve.json")
    throughput = _read_json(state_dir / "throughput_guard.json")
    ml_health = _read_json(state_dir / "ml_health.json")

    anomalies = list(log_watch.get("anomalies") or [])
    phase = str(pnl.get("last_phase") or "")
    vert = float(pnl.get("last_verticality") or 0.0)
    if phase in ("declining", "flat"):
        anomalies.append(f"curve:{phase}")
    if vert < 0.15 and phase != "vertical":
        anomalies.append(f"low_verticality:{vert:.3f}")

    opens_60m = int(throughput.get("opens_60m") or 0)
    target = int(throughput.get("target_opens_hr") or 0)
    if target and opens_60m < max(1, target // 2):
        anomalies.append(f"low_tph:{opens_60m}/{target}")

    if not ml_health.get("ok", True):
        anomalies.extend([f"ml:{i}" for i in (ml_health.get("issues") or [])[:2]])

    report = {
        "ts": now,
        "equity": log_watch.get("equity", 0),
        "open": log_watch.get("open", 0),
        "curve_phase": phase,
        "verticality": vert,
        "opens_60m": opens_60m,
        "target_opens_hr": target,
        "stack": stack_note,
        "log_watch_actions": log_watch.get("actions") or [],
        "anomalies": anomalies,
        "ml_health_ok": ml_health.get("ok", True),
        "throughput_starved": bool(throughput.get("starved")),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    _touch_agent_flag(now, anomalies)
    _log(
        f"tick equity={report['equity']:.2f} phase={phase} "
        f"opens={opens_60m}/{target} anomalies={len(anomalies)}"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
