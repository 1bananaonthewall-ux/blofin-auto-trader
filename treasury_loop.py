#!/usr/bin/env python3
"""Endless $100 consolidation loop: scan wallets → sweep to Blofin → repeat."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from treasury.consolidator import run_cycle
from treasury.settings import load_treasury_settings


def _setup_logging(log_path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="Blofin treasury consolidator loop")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    settings = load_treasury_settings()
    _setup_logging(settings.log_path)

    if not settings.api_key:
        logging.error("Missing BLOFIN_API_KEY in .env")
        sys.exit(1)

    if not settings.wallets_path.exists():
        logging.error(
            "Create %s from treasury/wallets.example.json with your wallet addresses",
            settings.wallets_path,
        )
        sys.exit(1)

    if not settings.deposit_address:
        logging.warning(
            "BLOFIN_USDT_DEPOSIT_ADDRESS not set - sweeps will log only until you add it "
            "(Blofin: Assets > Deposit > USDT > %s)",
            settings.deposit_chain,
        )

    logging.info(
        "treasury loop start target=$%.0f poll=%ds dry_run=%s",
        settings.sweep_target_usd,
        settings.poll_seconds,
        settings.dry_run,
    )

    while True:
        try:
            run_cycle(settings)
        except FileNotFoundError as e:
            logging.error("%s", e)
            sys.exit(1)
        except Exception:
            logging.exception("treasury cycle failed")

        if args.once:
            break
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
