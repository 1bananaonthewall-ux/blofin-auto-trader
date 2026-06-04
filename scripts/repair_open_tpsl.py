"""One-shot: attach exchange TP/SL to every open position missing live pending orders."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from position_registry import PositionRegistry
from tpsl_guard import pending_is_adequate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("repair_open_tpsl")


def main() -> int:
    settings = load_settings()
    ex = BlofinExchange(settings)
    ex.load()
    reg = PositionRegistry(settings.state_dir)
    positions = ex.fetch_all_positions()
    if not positions:
        log.info("No open positions.")
        return 0

    fixed = 0
    failed = 0
    for sym, pos in positions.items():
        trade = str(pos.get("symbol") or sym).split("#", 1)[0]
        side = str(pos.get("side") or "")
        entry = float(pos.get("entry_price") or 0)
        contracts = float(pos.get("contracts") or 0)
        if not side or entry <= 0 or contracts <= 0:
            continue
        from markets import symbol_to_inst_id

        inst = symbol_to_inst_id(trade)
        ps = ex._position_side_for_order(side, pos)
        _, pending = ex._pending_tpsl(
            inst, side, entry, position_side=ps, allow_registry_fallback=False, retries=3
        )
        if pending.live_rows > 0 and pending_is_adequate(side, entry, pending):
            log.info("%s %s — TP/SL already on exchange", trade.split("/")[0], side)
            continue
        ex._clear_tpsl_trust(trade)
        ex._tpsl_repair_at.pop(ex._canonical_symbol(trade), None)
        meta = reg.get(trade) or {}
        take = float(meta.get("take_pct") or pos.get("take_pct") or 0.022)
        lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
        ok, sp, tp = ex.repair_position_tpsl(
            trade,
            side,
            contracts,
            take_pct=take,
            configured_leverage=lev,
            dry_run=settings.dry_run,
            cancel_existing=False,
            registry_meta=meta,
        )
        tag = trade.split("/")[0]
        if ok:
            fixed += 1
            log.info("%s %s — repaired stop=%.2f%% take=%.2f%%", tag, side, sp * 100, tp * 100)
        else:
            failed += 1
            log.warning("%s %s — repair failed (check margin/liq room in bot.log)", tag, side)

    log.info("Done: %d repaired, %d failed, %d open", fixed, failed, len(positions))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
