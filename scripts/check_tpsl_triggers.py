"""Debug: mark vs SL/TP triggers and recent TPSL history."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from exchange_client import BlofinExchange
from liquidation_guard import sl_tp_from_exchange_liq
from markets import symbol_to_inst_id
from tpsl_guard import adjust_triggers_for_market

s = load_settings()
ex = BlofinExchange(s)
ex.load()
for sym, pos in ex.fetch_all_positions().items():
    trade = str(pos.get("symbol") or sym).split("#", 1)[0]
    side = str(pos.get("side") or "")
    entry = float(pos.get("entry_price") or 0)
    liq = float(pos.get("liquidation_price") or 0)
    mark = ex._mark_for_symbol(trade, pos)
    sl, tp, sp, tpct = sl_tp_from_exchange_liq(
        side, entry, liq, 0.022, buffer=s.sl_liq_buffer, min_rr=3.0, enforce_tp_from_sl=True
    )
    sl, tp = adjust_triggers_for_market(side, sl, tp, mark)
    inst = symbol_to_inst_id(trade)
    print(f"{trade.split('/')[0]} {side} entry={entry:.6f} mark={mark:.6f} liq={liq:.6f}")
    print(f"  sl={sl:.6f} tp={tp:.6f} stop%={sp*100:.2f} take%={tpct*100:.2f}")
    if side == "long":
        print(f"  SL would fire now: {mark <= sl}  TP would fire now: {mark >= tp}")
    else:
        print(f"  SL would fire now: {mark >= sl}  TP would fire now: {mark <= tp}")
    hist = ex.http.get_tpsl_history(inst_id=inst, limit=5) or []
    for row in hist[:3]:
        print(
            f"  hist state={row.get('state')} sl={row.get('slTriggerPrice')} tp={row.get('tpTriggerPrice')}"
        )
