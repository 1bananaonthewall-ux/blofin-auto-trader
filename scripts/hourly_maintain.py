#!/usr/bin/env python3

"""

Hourly Blofin maintenance — health snapshot, 50x compliance, hourly brain.



  python scripts/hourly_maintain.py

  python scripts/hourly_maintain.py --no-close

  python scripts/hourly_maintain.py --brain-only

"""



from __future__ import annotations



import argparse

import json

import subprocess

import sys

import time

from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))



from config import load_settings

from exchange_client import BlofinExchange

from leverage_intel import leverage_needs_reentry

from position_registry import PositionRegistry



MISSION_LEV = 50





def _log_line(state_dir: Path, record: dict) -> None:

    path = state_dir / "hourly_agent_log.jsonl"

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:

        f.write(json.dumps(record) + "\n")





def _is_compliant(ex: BlofinExchange, sym: str, pos: dict, *, mission: int = MISSION_LEV) -> tuple[bool, str]:

    cap = ex.symbol_leverage_cap(sym)

    exch_max = ex.leverage_intel.exchange_max(sym) or cap

    target = min(mission, cap)

    inst = int(pos.get("leverage") or 0)

    eff = int(pos.get("effective_leverage") or inst)

    needs, reason = leverage_needs_reentry(

        pos, target_lev=mission, exchange_max=exch_max

    )

    if needs:

        return False, reason

    if inst < target - 1 or eff < target - 1:

        return False, f"inst={inst}x eff={eff}x need >={target}x"

    return True, ""





def close_non_compliant(

    ex: BlofinExchange,

    settings,

    registry: PositionRegistry,

    *,

    dry_run: bool,

) -> list[str]:

    closed: list[str] = []

    positions = ex.fetch_all_positions()

    for sym, pos in list(positions.items()):

        ok, reason = _is_compliant(ex, sym, pos)

        if ok:

            continue

        if dry_run:

            closed.append(f"DRY {sym.split('/')[0]} ({reason})")

            continue

        try:

            ex.cancel_pending_tpsl(sym)

            ex.close_position(sym, pos, False)

            registry.remove(sym)

            closed.append(f"{sym.split('/')[0]} ({reason})")

            time.sleep(0.25)

        except Exception as exc:

            closed.append(f"FAIL {sym.split('/')[0]}: {exc}")

    return closed





def main() -> int:

    ap = argparse.ArgumentParser()

    ap.add_argument("--no-close", action="store_true", help="Report only, no closes")

    ap.add_argument("--brain-only", action="store_true", help="Skip closes; run hourly brain only")

    args = ap.parse_args()



    settings = load_settings()

    state_dir = settings.state_dir

    ex = BlofinExchange(settings)

    ex.load()

    registry = PositionRegistry(state_dir)



    subprocess.run(

        [sys.executable, str(ROOT / "scripts" / "hourly_health_report.py")],

        cwd=str(ROOT),

        check=False,

    )



    report_path = state_dir / "hourly_report.json"

    report = json.loads(report_path.read_text(encoding="utf-8"))



    closed: list[str] = []

    if not args.no_close and not args.brain_only:

        closed = close_non_compliant(ex, settings, registry, dry_run=settings.dry_run)



    brain_note = "disabled"

    brain_policy = ""

    brain_applied: list[str] = []

    if getattr(settings, "hourly_brain_enabled", True):

        try:

            from dataclasses import asdict



            from hourly_brain import run_hourly_brain



            brain = run_hourly_brain(settings, ex, registry, report, root=ROOT)

            brain_policy = brain.policy

            brain_applied = brain.applied

            brain_note = (

                f"policy={brain.policy} autocode={brain.autocode_mode} "

                f"decided={len(brain.decided)} applied={len(brain.applied)}"

            )

            if brain.snapshot.anomalies:

                print("brain_anomalies:", "; ".join(brain.snapshot.anomalies[:5]))

            if brain.applied:

                print("hourly_brain:", "; ".join(brain.applied[:8]))

        except Exception as exc:

            brain_note = f"err:{exc}"

            print("hourly_brain error:", exc)

    else:

        print("hourly_brain: disabled (HOURLY_BRAIN_ENABLED=false)")



    record = {

        "ts": time.time(),

        "equity": report.get("equity"),

        "open": report.get("open_count"),

        "closed": closed,

        "hourly_brain": brain_note,

        "brain_policy": brain_policy,

        "brain_applied": brain_applied,

        "dry_run": settings.dry_run,

    }

    _log_line(state_dir, record)



    stamp = state_dir / "last_cursor_hourly.txt"

    stamp.write_text(str(time.time()), encoding="utf-8")

    due = ROOT / ".cursor" / "HOURLY_DUE"

    if due.is_file():

        due.unlink()



    print("=== hourly maintain ===")

    print(f"equity=${report.get('equity')} open={report.get('open_count')}")

    if closed:

        print("closed:", ", ".join(closed))

    else:

        print("closed: (none)")

    print("hourly_brain:", brain_note)

    return 0





if __name__ == "__main__":

    raise SystemExit(main())


