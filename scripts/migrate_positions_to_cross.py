#!/usr/bin/env python3
"""Re-open isolated positions under cross margin (close + market re-entry + TPSL)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from margin_migrator import list_isolated_positions, migrate_position_to_cross
from margin_mode import is_cross_margin
from position_registry import PositionRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log only, no orders")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    settings = load_settings()
    if not is_cross_margin(settings.margin_mode):
        print(f"MARGIN_MODE is {settings.margin_mode!r} — set MARGIN_MODE=cross first")
        return 1

    ex = BlofinExchange(settings)
    ex.load()
    dry = args.dry_run or settings.dry_run
    registry = PositionRegistry(settings.state_dir)

    ex.ensure_account_margin_mode()
    isolated = list_isolated_positions(ex)

    if not isolated:
        print("All open positions are already cross margin.")
        return 0

    print(f"Found {len(isolated)} isolated position(s) to migrate:")
    for key, pos, mm in isolated:
        sym = pos.get("symbol") or key
        print(
            f"  {sym} {pos.get('side')} contracts={pos.get('contracts')} "
            f"lev={pos.get('leverage')} marginMode={mm}"
        )

    if not args.yes and not dry:
        ans = input("Migrate now? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    migrated = 0
    for _key, pos, _mm in isolated:
        if dry:
            migrate_position_to_cross(ex, settings, registry, pos, dry_run=True)
            migrated += 1
            continue
        if migrate_position_to_cross(ex, settings, registry, pos, dry_run=False):
            migrated += 1

    left = list_isolated_positions(ex)
    if left:
        print("\nStill isolated:", json.dumps([(k, m) for k, _, m in left], indent=2))
        return 2
    print(f"\nDone — migrated {migrated}, all positions cross margin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
