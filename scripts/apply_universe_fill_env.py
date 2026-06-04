"""Unlimited hourly throughput + universe fill (margin is the only open limit)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

UPDATES = {
    "UNIVERSE_FILL_MODE": "true",
    "TRADE_UNIVERSE": "all",
    "MAX_POSITIONS": "0",
    "AUTO_MAX_POSITIONS": "false",
    "MAX_OPENS_PER_TICK": "12",
    "OPTIMIZER_TARGET_MAX_TPH": "9999",
    "OPTIMIZER_TARGET_MIN_TPH": "1",
    "SCALP_ENTRY_GAP_SECONDS": "6",
    "SCALP_COOLDOWN_MINUTES": "2",
    "SYMBOLS_PER_TICK": "240",
    "MIN_MARGIN_RATE": "1.20",
    "TARGET_MARGIN_RATE": "1.32",
    "MAX_EFFECTIVE_LEVERAGE": "50",
    "RUNNER_PRIORITY_MODE": "true",
    "MOMENTUM_WAVE_MODE": "true",
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
    print("universe fill env:", ", ".join(sorted(UPDATES)))


if __name__ == "__main__":
    main()
