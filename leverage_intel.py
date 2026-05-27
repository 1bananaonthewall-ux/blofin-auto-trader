"""
Per-symbol leverage intelligence (free, local).

Blofin caps max leverage per instrument (e.g. 1000RATS max 40x). Setting 50x
returns 152002. Isolated positions can show inst=40x but eff=30x until
re-opened with correct margin — we detect that and let core brain re-enter.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from markets import symbol_to_inst_id

log = logging.getLogger(__name__)

_LADDER = (50, 45, 40, 35, 30, 25, 20, 15, 10, 5, 3, 2, 1)


def parse_instrument_max_leverage(inst: dict[str, Any]) -> int | None:
    for key in ("maxLeverage", "max_leverage", "lever", "maxLever"):
        raw = inst.get(key)
        if raw is None or raw == "":
            continue
        try:
            v = int(float(raw))
            if v >= 1:
                return v
        except (TypeError, ValueError):
            continue
    return None


def leverage_needs_reentry(
    pos: dict[str, Any],
    *,
    target_lev: int,
    exchange_max: int,
) -> tuple[bool, str]:
    """
    True when the position cannot reach mission leverage without a fresh entry.
    """
    inst = int(pos.get("leverage") or 0)
    eff = int(pos.get("effective_leverage") or inst or 0)
    cap = max(1, min(target_lev, exchange_max))

    if inst > 0 and inst < cap - 1:
        return True, f"instrument {inst}x < symbol cap {cap}x"

    # Leverage setting raised on exchange but isolated margin still sized for old lev.
    if inst >= cap - 1 and eff > 0 and eff < min(inst, cap) - 2:
        return True, f"stale margin eff={eff}x inst={inst}x cap={cap}x (re-enter)"

    notional = float(pos.get("notional_usdt") or 0)
    margin = float(pos.get("margin_usdt") or 0)
    if notional > 0 and margin > 0 and inst >= cap - 1:
        implied = int(round(notional / margin))
        if implied < cap - 2:
            return True, f"implied {implied}x < cap {cap}x (re-enter at max)"

    return False, ""


class LeverageIntel:
    """Cache exchange max leverage per instId; smart set-leverage with fallback."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._max_by_inst: dict[str, int] = {}
        self._set_by_inst: dict[str, int] = {}
        self._cache_path = (state_dir / "leverage_caps.json") if state_dir else None
        if self._cache_path and self._cache_path.is_file():
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                self._max_by_inst = {k: int(v) for k, v in (data.get("max") or {}).items()}
            except Exception:
                pass

    def ingest_instruments(self, instruments: list[dict[str, Any]]) -> int:
        n = 0
        for inst in instruments:
            inst_id = inst.get("instId") or ""
            if not inst_id:
                continue
            mx = parse_instrument_max_leverage(inst)
            if mx:
                self._max_by_inst[inst_id] = mx
                n += 1
        self._persist()
        return n

    def _persist(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"max": self._max_by_inst, "ts": time.time()}, indent=0),
                encoding="utf-8",
            )
        except Exception:
            pass

    def exchange_max(self, symbol: str) -> int | None:
        return self._max_by_inst.get(symbol_to_inst_id(symbol))

    def resolve_target(self, symbol: str, desired: int) -> int:
        mx = self.exchange_max(symbol)
        if mx is None:
            return max(1, desired)
        return max(1, min(desired, mx))

    def last_set(self, symbol: str) -> int | None:
        return self._set_by_inst.get(symbol_to_inst_id(symbol))

    def ensure(
        self,
        http: Any,
        symbol: str,
        *,
        desired: int,
        position_side: str = "net",
        cancel_tpsl_fn: Any | None = None,
    ) -> int:
        """
        Set leverage to min(desired, exchange max). Steps down on 152002.
        Returns leverage applied (0 if all failed).
        """
        inst_id = symbol_to_inst_id(symbol)
        cap = self.exchange_max(symbol)
        target = self.resolve_target(symbol, desired)

        if cancel_tpsl_fn:
            try:
                cancel_tpsl_fn(symbol)
            except Exception:
                pass

        tries: list[int] = []
        for lev in _LADDER:
            if lev <= target and lev not in tries:
                tries.append(lev)
        if target not in tries:
            tries.insert(0, target)

        last_err: Exception | None = None
        for lev in tries:
            try:
                http.set_leverage(inst_id, lev, position_side=position_side)
                self._set_by_inst[inst_id] = lev
                if cap and lev < desired:
                    log.warning(
                        "%s exchange cap %dx — set %dx (mission %dx)",
                        symbol.split("/")[0],
                        cap,
                        lev,
                        desired,
                    )
                elif lev < target:
                    log.info("%s leverage set %dx", symbol.split("/")[0], lev)
                return lev
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                if "152002" in msg or "Parameter leverage" in msg:
                    continue
                log.warning("set_leverage %s %dx: %s", symbol.split("/")[0], lev, exc)
                break

        if last_err:
            log.warning(
                "set_leverage %s failed (wanted %dx cap=%s): %s",
                symbol,
                target,
                cap,
                last_err,
            )
        return 0
