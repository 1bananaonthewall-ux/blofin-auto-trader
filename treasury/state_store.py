from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class TreasuryState:
    last_scan_ts: float = 0.0
    last_total_usd: float = 0.0
    sweeps_completed: int = 0
    total_swept_usd: float = 0.0
    last_sweep_ts: float = 0.0
    seen_deposit_ids: list[str] | None = None
    pending_sweep_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.seen_deposit_ids is None:
            self.seen_deposit_ids = []


def load_state(path: Path) -> TreasuryState:
    if not path.exists():
        return TreasuryState()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return TreasuryState(
        last_scan_ts=float(raw.get("last_scan_ts", 0)),
        last_total_usd=float(raw.get("last_total_usd", 0)),
        sweeps_completed=int(raw.get("sweeps_completed", 0)),
        total_swept_usd=float(raw.get("total_swept_usd", 0)),
        last_sweep_ts=float(raw.get("last_sweep_ts", 0)),
        seen_deposit_ids=list(raw.get("seen_deposit_ids") or []),
        pending_sweep_usd=float(raw.get("pending_sweep_usd", 0)),
    )


def save_state(path: Path, state: TreasuryState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
