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

from margin_mode import normalize_margin_mode
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

    # Do not force re-entry to "mission 50x" when anti-liq cap is lower — only if dangerously over-levered.
    if inst > 0 and eff > cap + 8:
        return True, f"effective {eff}x >> safe cap {cap}x"

    notional = float(pos.get("notional_usdt") or 0)
    margin = float(pos.get("margin_usdt") or 0)
    if notional > 0 and margin > 0:
        implied = int(round(notional / margin))
        if implied > cap + 10:
            return True, f"implied {implied}x >> safe cap {cap}x"

    return False, ""


class LeverageIntel:
    """Cache exchange max leverage per instId; smart set-leverage with fallback."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._max_by_inst: dict[str, int] = {}
        # instId -> {"lev": int, "margin_mode": "cross"|"isolated"}
        self._set_by_inst: dict[str, dict[str, Any]] = {}
        self._last_set_attempt: dict[str, float] = {}
        self._min_set_interval_sec = 45.0
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
        entry = self._set_by_inst.get(symbol_to_inst_id(symbol))
        if isinstance(entry, dict):
            lev = int(entry.get("lev") or 0)
            return lev if lev > 0 else None
        if isinstance(entry, int) and entry > 0:
            return entry
        return None

    def invalidate_leverage_cache(self, inst_id: str | None = None) -> None:
        """Force set-leverage on next open (required after cross/isolated switch)."""
        if inst_id:
            self._set_by_inst.pop(inst_id, None)
            self._last_set_attempt.pop(inst_id, None)
        else:
            self._set_by_inst.clear()
            self._last_set_attempt.clear()

    def _cached_set(self, inst_id: str) -> tuple[int, str] | None:
        entry = self._set_by_inst.get(inst_id)
        if isinstance(entry, dict):
            lev = int(entry.get("lev") or 0)
            mm = normalize_margin_mode(entry.get("margin_mode"))
            if lev > 0:
                return lev, mm
        if isinstance(entry, int) and entry > 0:
            return entry, "isolated"
        return None

    def ensure(
        self,
        http: Any,
        symbol: str,
        *,
        desired: int,
        position_side: str = "net",
        margin_mode: str = "isolated",
        cancel_tpsl_fn: Any | None = None,
    ) -> int:
        """
        Set leverage to min(desired, exchange max). Steps down on 152002.
        Returns leverage applied (0 if all failed).
        """
        inst_id = symbol_to_inst_id(symbol)
        cap = self.exchange_max(symbol)
        target = self.resolve_target(symbol, desired)
        want_mm = normalize_margin_mode(margin_mode)
        now = time.time()
        cached = self._cached_set(inst_id)
        if (
            cached
            and cached[0] == target
            and cached[1] == want_mm
            and (now - self._last_set_attempt.get(inst_id, 0)) < self._min_set_interval_sec
        ):
            return target

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
                self._last_set_attempt[inst_id] = time.time()
                http.set_leverage(
                    inst_id,
                    lev,
                    position_side=position_side,
                    margin_mode=want_mm,
                )
                self._set_by_inst[inst_id] = {"lev": lev, "margin_mode": want_mm}
                time.sleep(0.35)
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
                if "110012" in msg or "too frequent" in msg.lower():
                    hit = self._cached_set(inst_id)
                    if hit and hit[1] == want_mm:
                        log.debug(
                            "set_leverage %s rate-limited — using cached %dx %s",
                            symbol.split("/")[0],
                            hit[0],
                            want_mm,
                        )
                        return hit[0]
                    time.sleep(1.2)
                    continue
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
