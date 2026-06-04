#!/usr/bin/env python3
"""Download recommended GGUF for local cortex (resumable)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    # 3B Q4 — fast + much smarter than HF 0.5B (~2GB)
    "3b": (
        "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        "qwen2.5-3b-instruct-q4_k_m.gguf",
    ),
    # 7B Q3 — best quality if you have VRAM/RAM (~3.6GB)
    "7b": (
        "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q3_k_m.gguf",
        "qwen2.5-7b-instruct-q3_k_m.gguf",
    ),
    "14b": (
        "https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q3_k_m.gguf",
        "qwen2.5-14b-instruct-q3_k_m.gguf",
    ),
}


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        print(f"exists: {dest}")
        return
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {}
    mode = "wb"
    if part.is_file():
        headers["Range"] = f"bytes={part.stat().st_size}-"
        mode = "ab"
        print(f"resume: {part.stat().st_size} bytes")
    with requests.get(url, stream=True, headers=headers, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        with open(part, mode) as fh:
            done = part.stat().st_size if part.is_file() else 0
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r{done * 100 // (done + total - done + 1)}%", end="", flush=True)
    part.replace(dest)
    print(f"\nsaved: {dest}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=("3b", "7b", "14b"), default="3b")
    args = ap.parse_args()
    url, name = MODELS[args.model]
    dest = ROOT / "models" / name
    download(url, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
