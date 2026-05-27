#!/usr/bin/env python3
"""
Zero-fee capital acquisition checklist — prioritizes tasks, no fake money generation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACQ_PATH = Path(
    __import__("os").environ.get("CAPITAL_ACQUISITION_JSON", str(ROOT / "treasury" / "acquisition.json"))
)
ACQ_STATE = ROOT / "treasury" / "state" / "acquisition_done.json"


@dataclass
class Channel:
    id: str
    type: str
    name: str
    url: str
    priority: int
    estimated_usd: float
    effort_minutes: int
    enabled: bool
    recur_days: float
    notes: str


def load_channels(path: Path) -> tuple[list[Channel], dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy treasury/acquisition.example.json to treasury/acquisition.json"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    channels: list[Channel] = []
    for row in raw.get("channels", []):
        channels.append(
            Channel(
                id=str(row.get("id", "")),
                type=str(row.get("type", "")),
                name=str(row.get("name", "")),
                url=str(row.get("url", "")),
                priority=int(row.get("priority", 99)),
                estimated_usd=float(row.get("estimated_usd", 0)),
                effort_minutes=int(row.get("effort_minutes", 10)),
                enabled=bool(row.get("enabled", False)),
                recur_days=float(row.get("recur_days", 7)),
                notes=str(row.get("notes", "")),
            )
        )
    meta = {
        "daily_time_budget_minutes": int(raw.get("daily_time_budget_minutes", 30)),
        "target_batch_usd": float(raw.get("target_batch_usd", 100)),
    }
    return channels, meta


def load_done() -> dict[str, float]:
    if not ACQ_STATE.exists():
        return {}
    return json.loads(ACQ_STATE.read_text(encoding="utf-8"))


def mark_done(channel_id: str) -> None:
    state = load_done()
    state[channel_id] = time.time()
    ACQ_STATE.parent.mkdir(parents=True, exist_ok=True)
    ACQ_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"Marked done: {channel_id}")


def affiliate_snapshot() -> str | None:
    try:
        from config import load_settings
        from blofin_http import BlofinHttp

        s = load_settings()
        if not s.api_key:
            return None
        h = BlofinHttp(s.api_key, s.secret, s.passphrase)
        basic = h.request("GET", "/api/v1/affiliate/basic")
        codes = h.request("GET", "/api/v1/affiliate/referral-code")
        if isinstance(basic, dict):
            commission = basic.get("totalCommission") or basic.get("totalCommision")
            invitees = basic.get("tradeInvitees") or basic.get("subInvitees")
            code_hint = ""
            if isinstance(codes, list) and codes:
                code_hint = f" | codes={len(codes)}"
            return f"Blofin affiliate: commission={commission} traded_invitees={invitees}{code_hint}"
    except Exception as e:
        return f"Blofin affiliate API: {type(e).__name__} (may not be enrolled)"
    return None


def blofin_equity_line() -> str | None:
    try:
        from config import load_settings
        from blofin_http import BlofinHttp

        s = load_settings()
        h = BlofinHttp(s.api_key, s.secret, s.passphrase)
        bal = h.get_balance("futures")
        if isinstance(bal, dict):
            eq = float(bal.get("totalEquity") or 0)
            bonus = 0.0
            details = bal.get("details") or []
            if details and isinstance(details[0], dict):
                try:
                    bonus = float(details[0].get("bonus") or 0)
                except (TypeError, ValueError):
                    pass
            return f"Blofin live futures equity=${eq:.2f} bonus=${bonus:.2f}"
    except Exception:
        return None
    return None


def run_report(mark_done_id: str | None = None) -> int:
    if mark_done_id:
        mark_done(mark_done_id)
        return 0

    try:
        channels, meta = load_channels(ACQ_PATH)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    done = load_done()
    now = time.time()
    enabled = [c for c in channels if c.enabled]
    due: list[Channel] = []
    for c in enabled:
        last = done.get(c.id, 0.0)
        if now - last >= c.recur_days * 86400:
            due.append(c)

    due.sort(key=lambda x: (x.priority, -x.estimated_usd))

    print("=" * 60)
    print("ZERO-FEE CAPITAL PIPELINE (checklist - you execute, bot routes)")
    print("=" * 60)
    eq = blofin_equity_line()
    if eq:
        print(eq)
    aff = affiliate_snapshot()
    if aff:
        print(aff)
    print(f"Target batch: ${meta['target_batch_usd']:.0f} | Daily time budget: {meta['daily_time_budget_minutes']} min")
    print()

    if not due:
        print("No acquisition channels due. Run faucet_tracker.py for faucets.")
        print("  Mark complete: python capital_pipeline.py --done <channel-id>")
        return 0

    total_est = sum(c.estimated_usd for c in due)
    total_min = sum(c.effort_minutes for c in due)
    print(f"DUE NOW: {len(due)} channels | ~${total_est:.0f} potential | ~{total_min} min\n")

    for i, c in enumerate(due, 1):
        print(f"{i}. [{c.type}] {c.name}  (priority {c.priority}, ~${c.estimated_usd})")
        if c.url:
            print(f"   {c.url}")
        print(f"   Effort: ~{c.effort_minutes} min | {c.notes}")
        print(f"   When done: python capital_pipeline.py --done {c.id}\n")

    print("--- Also run ---")
    print("  python faucet_tracker.py")
    print("  python treasury_loop.py --once")
    print()
    print("Read ZERO_CAPITAL.md for limits. No method creates $100 instantly from $0.")

    if total_min > meta["daily_time_budget_minutes"]:
        print(
            f"\nWarning: due work (~{total_min} min) exceeds daily budget "
            f"({meta['daily_time_budget_minutes']} min). Do highest priority first."
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero-fee capital acquisition checklist")
    parser.add_argument("--done", metavar="ID", help="Mark channel completed")
    args = parser.parse_args()
    sys.exit(run_report(args.done))


if __name__ == "__main__":
    main()
