"""
Safe .env patches applied by LLM overseer guard (allowlisted keys only).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Keys the overseer may change without user confirmation.
ALLOWED_ENV: dict[str, set[str] | None] = {
    "LLM_ONLY_TRADING": {"false", "true"},
    "LLM_OVERSEER_MODE": {"true", "false"},
    "LLM_TRADING_ENABLED": {"true", "false"},
    "WHATSAPP_LLM_PROVIDER": {"hf_local", "llama_cpp", "auto"},
    "WHATSAPP_LLM_SKIP_LLAMA": {"true", "false"},
    "WHATSAPP_LLM_HF_MODEL": None,
    "SIGNAL_MODE": {"ml", "enhanced"},
    "ML_CONTINUOUS_TRAIN": {"true", "false"},
    "ML_AUTO_REFIT_ON_STARTUP": {"true", "false"},
    "HOURLY_3R_WINNER_MODE": {"true", "false"},
    "WINNER_ONLY_MODE": {"true", "false"},
    "WINNER_ELITE_ONLY": {"true", "false"},
    "QUALITY_PICK_MODE": {"true", "false"},
    "PICK_MIN_SCORE": None,
    "LLM_COPILOT_TRADING": {"true", "false"},
    "LLM_COPILOT_STRICT": {"true", "false"},
    "LLM_TRADING_ENABLED": {"true", "false"},
    "MOON_SWARM_ENABLED": {"true", "false"},
    "SYMBOLS_PER_TICK": None,
    "OPTIMIZER_TARGET_MIN_TPH": None,
    "OPTIMIZER_AUTOCODE_ENABLED": {"true", "false"},
    "ENTRIES_PAUSED": {"false", "true"},
    "MARKOV_REGIME_ENABLED": {"true", "false"},
    "RUNNER_FILTER_ENABLED": {"true", "false"},
}


def patch_env(env_path: Path, changes: dict[str, str]) -> list[str]:
    """Apply allowlisted env changes. Returns list of applied keys."""
    if not env_path.is_file():
        return []
    applied: list[str] = []
    for key, value in changes.items():
        if key not in ALLOWED_ENV:
            log.debug("overseer env skip disallowed key %s", key)
            continue
        allowed = ALLOWED_ENV[key]
        val = str(value).strip()
        if allowed is not None and val.lower() not in {a.lower() for a in allowed}:
            log.debug("overseer env skip value %s=%s", key, val)
            continue
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        filtered = [ln for ln in lines if not re.match(rf"^{re.escape(key)}=", ln)]
        filtered.append(f"{key}={val}")
        try:
            env_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
            applied.append(key)
            log.warning("OVERSEER ENV: %s=%s", key, val)
        except OSError as exc:
            log.warning("OVERSEER ENV patch failed %s: %s", key, exc)
    return applied
