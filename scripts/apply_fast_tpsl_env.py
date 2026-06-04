#!/usr/bin/env python3
"""Fast fee-aware 3R TPSL on cross margin (no liq-gap brackets)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

UPDATES = {
    "MARGIN_MODE": "cross",
    "SCALP_3R_MODE": "true",
    "SCALP_FAST_3R": "true",
    "SCALP_SKIP_LIQ_TPSL": "true",
    "SCALP_FAST_MAX_STOP_PCT": "0.010",
    "SCALP_FAST_MAX_TAKE_PCT": "0.030",
    "SCALP_MOMENTUM_MAX_STOP_PCT": "0.014",
    "SCALP_3R_MIN_RR": "3.0",
    "SCALP_3R_HARVEST_MIN_R": "1.0",
    "SCALP_MIN_HOLD_SECONDS": "18",
    "SCALP_FEE_COVERAGE_MULT": "2.0",
    "STACK_WINNERS_MODE": "false",
    "RUNNER_PRIORITY_MODE": "true",
}


def main() -> None:
    if not ENV.is_file():
        print("no .env found")
        return
    lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else ""
        if key in UPDATES:
            out.append(f"{key}={UPDATES[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in UPDATES.items():
        if key not in seen:
            out.append(f"{key}={val}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("fast TPSL env:", ", ".join(sorted(UPDATES)))


if __name__ == "__main__":
    main()
