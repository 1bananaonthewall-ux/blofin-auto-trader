from __future__ import annotations

import ast
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from local_llm import chat_completion, resolve_provider

log = logging.getLogger(__name__)

STATE_FILE = "optimizer_autocode_state.json"
OVERRIDES_FILE = "optimizer_overrides.py"
MAX_PATCH_CHARS = 3000


@dataclass
class MetricsSnapshot:
    action: str
    win_rate: float
    profit_factor: float
    equity_delta_15m_pct: float
    trades_last_hour: int


def _template(mode: str) -> str:
    if mode == "quality":
        # Pickier gates, stronger risk-off in stress.
        return """from __future__ import annotations

def apply_overrides(conf_gate: float, score_gate: float, *, markov_state: str = "", trades_last_hour: int = 0):
    conf_gate += 0.03
    score_gate += 2.0
    if markov_state == "stress":
        conf_gate += 0.03
        score_gate += 2.0
    return conf_gate, score_gate
"""
    if mode == "throughput":
        # Slightly looser to recover throughput, but still bounded.
        return """from __future__ import annotations

def apply_overrides(conf_gate: float, score_gate: float, *, markov_state: str = "", trades_last_hour: int = 0):
    conf_gate -= 0.02
    score_gate -= 1.5
    if markov_state == "stress":
        conf_gate += 0.02
    return conf_gate, score_gate
"""
    return """from __future__ import annotations

def apply_overrides(conf_gate: float, score_gate: float, *, markov_state: str = "", trades_last_hour: int = 0):
    # neutral mode
    return conf_gate, score_gate
"""


def _extract_python_block(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    # Prefer first fenced block content.
    parts = t.split("```")
    if len(parts) >= 3:
        body = parts[1]
        if body.startswith("python"):
            body = body[len("python") :]
        return body.strip()
    return t


def _safe_validate(code: str) -> bool:
    if len(code) > MAX_PATCH_CHARS:
        return False
    lower = code.lower()
    banned = ("import os", "import subprocess", "open(", "exec(", "eval(", "__import__", "socket", "requests")
    if any(b in lower for b in banned):
        return False
    try:
        mod = ast.parse(code)
    except Exception:
        return False
    funcs = [n for n in mod.body if isinstance(n, ast.FunctionDef)]
    return any(f.name == "apply_overrides" for f in funcs)


def _llm_generate(snapshot: MetricsSnapshot, mode: str) -> str | None:
    # Prefer OpenAI-compatible local server for optimizer codegen.
    base = (os.environ.get("OPTIMIZER_CODEGEN_BASE_URL") or "").strip().rstrip("/")
    model = (os.environ.get("OPTIMIZER_CODEGEN_MODEL") or "").strip() or "local-model"
    key = (os.environ.get("OPTIMIZER_CODEGEN_API_KEY") or "local").strip()

    system = (
        "Generate ONLY safe Python code for optimizer_overrides.py. "
        "No imports except __future__. "
        "Create function apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0). "
        "Return adjusted conf_gate, score_gate with small bounded deltas."
    )
    user = {
        "mode_hint": mode,
        "metrics": {
            "action": snapshot.action,
            "win_rate": snapshot.win_rate,
            "profit_factor": snapshot.profit_factor,
            "equity_delta_15m_pct": snapshot.equity_delta_15m_pct,
            "trades_last_hour": snapshot.trades_last_hour,
        },
        "constraints": {
            "abs_conf_delta_max": 0.08,
            "abs_score_delta_max": 4.0,
            "stress_should_tighten": True,
        },
    }
    text = None
    if base:
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(user)},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 320,
                },
                timeout=30,
            )
            r.raise_for_status()
            text = str(r.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            log.debug("optimizer openai_compat codegen failed: %s", exc)

    # Fallback to existing local_llm path if configured/available.
    if not text and resolve_provider() != "none":
        text, err = chat_completion(
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
            max_tokens=320,
            temperature=0.2,
        )
        if not text and err:
            log.debug("optimizer autocode llm err: %s", err)
            return None
    if not text:
        return None
    code = _extract_python_block(text)
    return code if _safe_validate(code) else None


def maybe_apply_autocode(
    state_dir: Path,
    *,
    enabled: bool,
    action: str,
    win_rate: float,
    profit_factor: float,
    equity_delta_15m_pct: float,
    trades_last_hour: int = 0,
    cooldown_sec: int = 900,
) -> str:
    """
    Rewrite optimizer_overrides.py when optimizer deems it useful.
    Returns mode string: unchanged|quality|throughput|neutral|disabled.
    """
    if not enabled:
        return "disabled"

    mode = "neutral"
    if action in {"tighten_quality", "slow_overtrade"} or win_rate < 0.40 or profit_factor < 0.85:
        mode = "quality"
    elif action in {"loosen_throughput", "accelerate_hot", "pace_up_quality"} and equity_delta_15m_pct > -2.5:
        mode = "throughput"

    st_path = state_dir / STATE_FILE
    now = time.time()
    prev = {"mode": "", "ts": 0.0}
    if st_path.exists():
        try:
            prev = json.loads(st_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if prev.get("mode") == mode and (now - float(prev.get("ts", 0.0))) < cooldown_sec:
        return "unchanged"

    snap = MetricsSnapshot(
        action=action,
        win_rate=win_rate,
        profit_factor=profit_factor,
        equity_delta_15m_pct=equity_delta_15m_pct,
        trades_last_hour=trades_last_hour,
    )
    code = _llm_generate(snap, mode) or _template(mode)
    (state_dir / OVERRIDES_FILE).write_text(code, encoding="utf-8")
    st_path.write_text(json.dumps({"mode": mode, "ts": now}, indent=2), encoding="utf-8")
    log.warning("OPTIMIZER AUTOCODE -> %s (rewrote %s)", mode, OVERRIDES_FILE)
    return mode
