#!/usr/bin/env python3
"""Apply Option B fast-LLM keys to .env (no secrets printed)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
GGUF_3B = ROOT / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf"
GGUF_7B = ROOT / "models" / "qwen2.5-7b-instruct-q3_k_m.gguf"

# If llama-cpp crashes on this CPU, auto still reaches hf_local after _LLAMA_BROKEN_UNTIL.
UPDATES = {
    "WHATSAPP_LLM_PROVIDER": "hf_local",
    "WHATSAPP_LLM_SKIP_LLAMA": "true",
    "WHATSAPP_LLM_FAST": "true",
    "WHATSAPP_LLM_POLICY_TIMEOUT_SEC": "12",
    "WHATSAPP_LLM_POLICY_MAX_TOKENS": "128",
    "WHATSAPP_LLM_POLICY_CONTEXT_CHARS": "3200",
    "WHATSAPP_LLM_HF_MODEL": "Qwen/Qwen2.5-1.5B-Instruct",
    "LLM_TRADING_ENABLED": "true",
    "LLM_TRADING_USE_CORTEX": "true",
    "LLM_TRADING_MAX_TOKENS": "128",
    "LLM_POLICY_CACHE_SEC": "45",
    "LOCAL_CORTEX_POLICY_MAX_KNOWLEDGE_CHARS": "1400",
    "DASHBOARD_COPILOT_MAX_TOKENS": "720",
    "DASHBOARD_LLM_TIMEOUT_SEC": "180",
    "DASHBOARD_LLM_WARMUP_WAIT_SEC": "300",
    "DASHBOARD_LLM_KEEPALIVE_SEC": "600",
}

if GGUF_3B.is_file():
    UPDATES["WHATSAPP_LLM_GGUF_PATH"] = "models/qwen2.5-3b-instruct-q4_k_m.gguf"
elif GGUF_7B.is_file():
    UPDATES["WHATSAPP_LLM_GGUF_PATH"] = "models/qwen2.5-7b-instruct-q3_k_m.gguf"

if not ENV.is_file():
    raise SystemExit(".env missing")

lines = ENV.read_text(encoding="utf-8").splitlines()
keys = set(UPDATES)
out: list[str] = []
for line in lines:
    if "=" in line and line.split("=", 1)[0].strip() in keys:
        continue
    out.append(line)
for k, v in UPDATES.items():
    out.append(f"{k}={v}")
ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
print("env_ok hf_local + SKIP_LLAMA (7B GGUF present; llama-cpp CPU wheel fails on this host)")
