"""
Detect conditions that block or starve trade flow — fed to LLM overseer guard cycles.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BLOCKERS_FILE = "overseer_blockers.json"


def _tail_log_lines(log_path: Path, n: int = 400) -> list[str]:
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _count_patterns(lines: list[str], patterns: tuple[str, ...]) -> int:
    return sum(1 for ln in lines if any(p in ln for p in patterns))


def detect_blockers(
    state_dir: Path,
    log_dir: Path,
    *,
    settings: Any = None,
    knobs: Any = None,
    ml_ready: bool = True,
    opens_last_hour: int = 0,
    opens_allowed: bool = True,
    api_paused: bool = False,
) -> dict[str, Any]:
    """Runtime + log snapshot of trade-flow blockers."""
    lines = _tail_log_lines(log_dir / "bot.log", 500)
    recent = lines[-200:]

    llama_fail = _count_patterns(
        recent,
        ("llama_cpp load failed", "0xc000001d", "illegal instruction"),
    )
    llm_only_slow = _count_patterns(recent, ("LLM-ONLY PORTAL", "LLM-ONLY opens", "CORTEX POLICY"))
    skip_heavy = _count_patterns(
        recent,
        ("skip ", "WINNER skip", "SWARM skip", "MARKOV skip", "no open:"),
    )
    open_ok = _count_patterns(recent, ("opened ", "entry ", "ORDER"))
    tpsl_block = _count_patterns(recent, ("TP/SL missing", "unprotected", "forcing repair"))
    entries_paused = _count_patterns(recent, ("entries paused", "entries gated", "entry pacer"))

    issues: list[dict[str, str]] = []

    if getattr(settings, "llm_only_trading", False):
        issues.append(
            {
                "id": "llm_only_per_symbol",
                "severity": "high",
                "detail": "LLM-only scans each symbol with slow local LLM — starves opens",
            }
        )
    if llama_fail and getattr(settings, "llm_only_trading", False):
        issues.append(
            {
                "id": "llama_cpp_broken",
                "severity": "critical",
                "detail": "GGUF llama_cpp crashes on this CPU — use hf_local overseer",
            }
        )
    if opens_last_hour < 2 and skip_heavy > 8 and open_ok < 2:
        issues.append(
            {
                "id": "flow_starved",
                "severity": "high",
                "detail": f"opens/hr={opens_last_hour} skips={skip_heavy} recent_opens={open_ok}",
            }
        )
    if not ml_ready and getattr(settings, "signal_mode", "") == "ml":
        issues.append(
            {
                "id": "ml_not_ready",
                "severity": "medium",
                "detail": "Signal ML not deployed — confluence-only until refit",
            }
        )
    if api_paused:
        issues.append({"id": "api_backoff", "severity": "high", "detail": "Exchange API paused"})
    if not opens_allowed:
        issues.append({"id": "entry_pacer", "severity": "medium", "detail": "Entry pacer blocking opens"})
    if tpsl_block:
        issues.append({"id": "tpsl_repair", "severity": "high", "detail": "TP/SL repair blocking new entries"})
    if entries_paused:
        issues.append({"id": "entries_paused", "severity": "medium", "detail": "Mission/fluid paused entries"})
    if knobs is not None and getattr(knobs, "allow_new_entries", True) is False:
        issues.append({"id": "knobs_pause", "severity": "medium", "detail": "Runtime knobs disallow new entries"})
    if llm_only_slow and open_ok < 1 and len(recent) > 30:
        issues.append(
            {
                "id": "llm_approvals_no_fills",
                "severity": "high",
                "detail": "LLM approvals logged but no opens — scan cycle too slow or gate blocked",
            }
        )

    payload = {
        "ts": time.time(),
        "opens_last_hour": opens_last_hour,
        "ml_ready": ml_ready,
        "api_paused": api_paused,
        "opens_allowed": opens_allowed,
        "skip_count_recent": skip_heavy,
        "open_count_recent": open_ok,
        "issues": issues,
    }
    out = state_dir / BLOCKERS_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_blockers(state_dir: Path) -> dict[str, Any]:
    path = state_dir / BLOCKERS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
