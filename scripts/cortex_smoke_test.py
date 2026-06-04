#!/usr/bin/env python3
"""Quick local LLM + cortex smoke test."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from local_cortex import augmented_messages, train
from local_llm import chat_completion, gguf_path, resolve_provider, status_line


def main() -> int:
    if not gguf_path():
        print("ERROR: no GGUF model in models/")
        return 1
    summary = train()
    print("cortex:", summary.get("knowledge_chars"), "chars", summary.get("examples"), "examples")
    print("llm:", status_line())
    msgs = augmented_messages(
        "You are the Blofin co-pilot. Be concise.",
        "live test snapshot",
        [],
        "Summarize how stops work on this bot and what slcheck does.",
    )
    text, err = chat_completion(msgs, max_tokens=220, temperature=0.2)
    print("--- reply ---")
    print(text or err)
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
