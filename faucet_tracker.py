#!/usr/bin/env python3
"""Claim reminders for crypto faucets — no captcha automation."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAUCETS_PATH = Path(
    __import__("os").environ.get("TREASURY_FAUCETS_JSON", str(ROOT / "treasury" / "faucets.json"))
)
STATE_PATH = ROOT / "treasury" / "state" / "faucet_claims.json"


@dataclass
class Faucet:
    id: str
    name: str
    url: str
    asset: str
    claim_interval_hours: float
    min_withdraw_usd: float
    estimated_claim_usd: float
    payout_wallet_id: str
    enabled: bool
    notes: str = ""


def load_faucets(path: Path) -> list[Faucet]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy treasury/faucets.example.json to treasury/faucets.json"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[Faucet] = []
    for row in raw.get("faucets", []):
        out.append(
            Faucet(
                id=str(row.get("id", "")),
                name=str(row.get("name", "")),
                url=str(row.get("url", "")),
                asset=str(row.get("asset", "USDT")).upper(),
                claim_interval_hours=float(row.get("claim_interval_hours", 24)),
                min_withdraw_usd=float(row.get("min_withdraw_usd", 0)),
                estimated_claim_usd=float(row.get("estimated_claim_usd", 0)),
                payout_wallet_id=str(row.get("payout_wallet_id", "")),
                enabled=bool(row.get("enabled", False)),
                notes=str(row.get("notes", "")),
            )
        )
    return out


def load_claim_state() -> dict[str, float]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_claim_state(state: dict[str, float]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_claimed(faucet_id: str) -> None:
    state = load_claim_state()
    state[faucet_id] = time.time()
    save_claim_state(state)
    print(f"Marked claimed: {faucet_id}")


def projection(faucets: list[Faucet]) -> None:
    enabled = [f for f in faucets if f.enabled]
    if not enabled:
        print("No enabled faucets. Edit treasury/faucets.json and set enabled: true")
        return
    daily = sum(
        (24 / max(f.claim_interval_hours, 0.1)) * f.estimated_claim_usd for f in enabled
    )
    print(f"Enabled faucets: {len(enabled)}")
    print(f"Estimated gross: ${daily:.4f}/day  ${daily * 30:.2f}/month (before fees & skips)")
    for f in enabled:
        days_to_min = (
            f.min_withdraw_usd / ((24 / max(f.claim_interval_hours, 0.1)) * f.estimated_claim_usd)
            if f.estimated_claim_usd > 0
            else float("inf")
        )
        print(
            f"  {f.id}: {f.asset} ~${f.estimated_claim_usd}/claim -> "
            f"wallet {f.payout_wallet_id} min ${f.min_withdraw_usd} "
            f"(~{days_to_min:.0f} days to min withdraw if perfect)"
        )
    batches_per_month = (daily * 30) / 100
    print(f"At that rate: ~{batches_per_month:.2f} x $100 Blofin batches/month (theoretical max)")


def due_report(faucets: list[Faucet]) -> list[Faucet]:
    state = load_claim_state()
    now = time.time()
    due: list[Faucet] = []
    print("=== Faucet claim status ===\n")
    for f in faucets:
        if not f.enabled:
            continue
        last = state.get(f.id, 0.0)
        interval_s = f.claim_interval_hours * 3600
        elapsed = now - last if last else interval_s + 1
        is_due = elapsed >= interval_s
        if is_due:
            due.append(f)
        status = "DUE NOW" if is_due else f"next in {(interval_s - elapsed) / 3600:.1f}h"
        print(f"[{status}] {f.name} ({f.asset})")
        print(f"  {f.url}")
        print(f"  pays to wallet: {f.payout_wallet_id}  |  {f.notes}\n")
    if not due:
        print("Nothing due right now. Use --claimed <id> after you claim manually.")
    return due


def main() -> None:
    parser = argparse.ArgumentParser(description="Faucet claim reminders (manual claims)")
    parser.add_argument("--claimed", metavar="ID", help="Record that you claimed faucet ID now")
    parser.add_argument("--project", action="store_true", help="Show earnings projection")
    args = parser.parse_args()

    if args.claimed:
        mark_claimed(args.claimed)
        return

    try:
        faucets = load_faucets(FAUCETS_PATH)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if args.project:
        projection(faucets)
        return

    due_report(faucets)


if __name__ == "__main__":
    main()
