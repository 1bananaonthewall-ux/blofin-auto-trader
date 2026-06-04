#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from local_llm import gguf_path, resolve_provider, status_line

print("provider:", resolve_provider())
print("gguf:", gguf_path())
print(status_line())
