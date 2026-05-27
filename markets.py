from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    inst_id: str
    symbol: str
    min_size: float
    contract_size: float
    last_price: float
    max_leverage: int = 50

    @property
    def min_notional_usdt(self) -> float:
        return self.last_price * self.contract_size * self.min_size

    @property
    def min_margin_usdt(self) -> float:
        return self.min_notional_usdt


def inst_id_to_symbol(inst_id: str) -> str:
    base = inst_id.replace("-USDT", "")
    return f"{base}/USDT:USDT"


def symbol_to_inst_id(symbol: str) -> str:
    base = symbol.split(":")[0]
    return base.replace("/", "-")


def dynamic_max_positions(equity: float, hard_cap: int = 9999) -> int:
    """Scale slot count with equity; tiny accounts stay concentrated."""
    if equity <= 0:
        return 0
    if equity < 1:
        return min(hard_cap, int(equity * 100))
    if equity < 50:
        return min(hard_cap, max(2, int(equity // 8)))
    if equity < 200:
        return min(hard_cap, max(4, int(equity // 15)))
    return min(hard_cap, max(8, int(equity // 25)))


def compute_max_open_positions(
    equity: float,
    affordable_count: int,
    *,
    cap: int,
    auto_balance: bool,
    margin_utilization: float,
    markets: list[Market],
    leverage: int,
    min_equity_per_slot: float = 0.001,
) -> int:
    """No limit on positions - take every valid signal."""
    if equity <= 0 or affordable_count <= 0:
        return 0

    if not auto_balance:
        return min(cap, affordable_count)

    if not markets:
        return min(cap, affordable_count)

    # Dynamic min slot - extremely permissive for tiny accounts
    if equity < 10.0:
        dynamic_min_slot = max(0.01, equity * 0.05)
    else:
        dynamic_min_slot = max(0.1, equity * 0.01)

    margins = sorted(m.min_margin_usdt / max(leverage, 1) for m in markets)
    median_margin = margins[len(margins) // 2] if margins else 1.0
    if median_margin <= 0:
        return min(cap, affordable_count)

    budget = equity * margin_utilization
    slots_by_margin = max(1, int(budget // max(median_margin, 0.001)))
    slots_by_equity = max(1, int(equity // max(dynamic_min_slot, 0.001)))

    dynamic_cap = dynamic_max_positions(equity, hard_cap=cap)
    return max(1, min(dynamic_cap, affordable_count, slots_by_margin, slots_by_equity))


def portfolio_risk_pct(total_risk_pct: float, open_slots: int) -> float:
    if open_slots <= 0:
        return total_risk_pct
    return total_risk_pct / open_slots