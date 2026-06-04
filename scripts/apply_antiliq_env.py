"""Patch .env for margin-rate anti-liquidation (high leverage OK, extra collateral required)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

# Safety = MIN/TARGET margin rate on each open, not a low leverage cap.
UPDATES = {
    "MAX_EFFECTIVE_LEVERAGE": "50",
    "SCALP_LEVERAGE_MAX": "50",
    "MIN_MARGIN_RATE": "1.20",
    "TARGET_MARGIN_RATE": "1.32",
    "MARGIN_USE_FRACTION": "0.72",
    "MAX_STOP_LIQ_FRACTION": "0.24",
    "SL_LIQ_BUFFER": "0.50",
    "PRE_LIQUIDATION_EXIT_FACTOR": "0.60",
    "MARGIN_TOP_UP_ENABLED": "false",
    "MAX_OPENS_PER_TICK": "1",
    "SMALL_ACCOUNT_MAX_OPEN": "2",
    "SMALL_ACCOUNT_MAX_OPENS_PER_TICK": "1",
}


def main() -> None:
    if not ENV.is_file():
        print("no .env found")
        return
    lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set()
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
    print("margin-rate safety (not leverage caps):", ", ".join(sorted(UPDATES)))


if __name__ == "__main__":
    main()
