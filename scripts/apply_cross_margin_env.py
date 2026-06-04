"""Switch God Bot to cross margin (shared wallet collateral)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

UPDATES = {
    "MARGIN_MODE": "cross",
    "MARGIN_TOP_UP_ENABLED": "false",
    "MIN_MARGIN_RATE": "1.00",
    "TARGET_MARGIN_RATE": "1.08",
    "MAX_STOP_LIQ_FRACTION": "0.45",
    "AUTO_CROSS_MARGIN_MIGRATE": "true",
    "AUTO_CROSS_MARGIN_INTERVAL_SEC": "90",
    "AUTO_CROSS_MARGIN_SYMBOL_COOLDOWN_SEC": "300",
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
    print("cross margin env:", ", ".join(sorted(UPDATES)))


if __name__ == "__main__":
    main()
