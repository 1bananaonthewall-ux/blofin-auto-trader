#!/usr/bin/env python3
"""Watch bot.log for new errors (byte-offset). Usage: python scripts/watch_bot_log.py [minutes]"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "bot.log"
PATTERNS = re.compile(
    r"ERROR|Traceback|TypeError|steward cycle failed|PermissionError|"
    r"152002|margin_usdt|unexpected keyword|OPEN ABORTED|TP/SL still missing",
    re.I,
)
SKIP = re.compile(r"403 Forbidden|Handshake status 403", re.I)
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    if not LOG.is_file():
        print(f"Missing {LOG}")
        return 1
    offset = LOG.stat().st_size
    end = time.time() + minutes * 60
    seen: set[str] = set()
    print(f"Watch {minutes:.0f}m from offset={offset} ({time.strftime('%H:%M:%S')})")
    while time.time() < end:
        time.sleep(75)
        with LOG.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            chunk = f.read()
            offset = f.tell()
        if not chunk.strip():
            continue
        hits = []
        for line in chunk.splitlines():
            if not PATTERNS.search(line) or SKIP.search(line):
                continue
            key = line.strip()[:200]
            if key not in seen:
                seen.add(key)
                hits.append(line.strip())
        if hits:
            print(f"\n--- {time.strftime('%H:%M:%S')} {len(hits)} new ---")
            for h in hits[-15:]:
                print(h[:300])
    print(f"\nDONE unique={len(seen)}")
    for line in sorted(seen):
        print(line[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
