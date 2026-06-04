"""Keep open positions on cross margin (close isolated + reopen cross + TPSL)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from margin_mode import is_cross_margin, normalize_margin_mode
from markets import symbol_to_inst_id
from tpsl_guard import pct_from_prices

if TYPE_CHECKING:
    from config import Settings
    from exchange_client import BlofinExchange
    from position_registry import PositionRegistry

log = logging.getLogger(__name__)

STATE_FILE = "cross_margin_migrate.json"
DEFAULT_INTERVAL_SEC = 90.0
DEFAULT_SYMBOL_COOLDOWN_SEC = 300.0
MIN_RETRY_SEC = 45.0


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_isolated_positions(
    ex: BlofinExchange, positions: dict[str, dict[str, Any]] | None = None
) -> list[tuple[str, dict[str, Any], str]]:
    """Return [(slot_key, pos, margin_mode)] for non-cross rows."""
    rows = positions if positions is not None else ex.fetch_all_positions()
    out: list[tuple[str, dict[str, Any], str]] = []
    for key, pos in rows.items():
        mm = ex._margin_mode_for_position(pos)
        if mm != "cross":
            out.append((key, pos, mm))
    return out


def _registry_meta(registry: PositionRegistry, symbol: str) -> dict[str, Any]:
    data = registry._data
    base = symbol.split("#", 1)[0]
    return dict(data.get(symbol) or data.get(base) or {})


def migrate_position_to_cross(
    ex: BlofinExchange,
    settings: Settings,
    registry: PositionRegistry,
    pos: dict[str, Any],
    *,
    dry_run: bool = False,
) -> bool:
    """
    Close one isolated position and reopen the same size/side under cross margin.
    Returns True when the book shows cross (or dry-run would do so).
    """
    symbol = str(pos.get("symbol") or "")
    if not symbol:
        return False
    side = str(pos.get("side") or "long").lower()
    contracts = float(pos.get("contracts") or 0)
    lev = int(pos.get("leverage") or settings.leverage or 10)
    if contracts <= 0:
        return False

    mm = ex._margin_mode_for_position(pos)
    if mm == "cross":
        return True

    default_stop = float(settings.stop_loss_pct or 0.01)
    default_take = float(settings.take_profit_pct or 0.02)
    meta = _registry_meta(registry, symbol)
    stop_pct = float(meta.get("stop_pct") or default_stop)
    take_pct = float(meta.get("take_pct") or default_take)
    sl_p = float(meta.get("sl_price") or 0)
    tp_p = float(meta.get("tp_price") or 0)
    entry = float(pos.get("entry_price") or meta.get("entry_price") or 0)
    if sl_p > 0 and tp_p > 0 and entry > 0:
        sp, tp = pct_from_prices(side, entry, sl_p, tp_p)
        if sp > 0:
            stop_pct = sp
        if tp > 0:
            take_pct = tp

    short = symbol.split("/")[0]
    if dry_run:
        log.info(
            "DRY_RUN cross migrate %s %s size=%s (%s -> cross)",
            short,
            side,
            contracts,
            mm,
        )
        return True

    log.warning(
        "CROSS MIGRATE %s %s size=%s lev=%dx (%s -> cross)",
        short,
        side,
        contracts,
        lev,
        mm,
    )
    try:
        ex.cancel_pending_tpsl(symbol)
    except Exception as exc:
        log.debug("cross migrate cancel TPSL %s: %s", short, exc)

    ex.close_position(symbol, pos, dry_run=False)
    time.sleep(0.55)
    if ex._lookup_open_position(symbol, side):
        log.error("cross migrate %s: still open after close", short)
        return False

    ex.ensure_account_margin_mode()
    ex.leverage_intel.invalidate_leverage_cache(symbol_to_inst_id(symbol))
    result = ex.open_position(
        symbol,
        side,
        contracts,
        stop_pct,
        take_pct,
        dry_run=False,
        leverage=lev,
    )
    if not result:
        log.error("cross migrate %s reopen failed: %s", short, ex.last_open_error)
        return False

    time.sleep(0.45)
    reopened = ex._lookup_open_position(symbol, side)
    if not reopened:
        log.error("cross migrate %s: no position after reopen", short)
        return False
    got_mm = ex._margin_mode_for_position(reopened)
    if got_mm != "cross":
        log.error("cross migrate %s: reopened as %s not cross", short, got_mm)
        return False

    registry.record_open(
        symbol,
        side=side,
        entry_price=float(reopened.get("entry_price") or entry),
        leverage=lev,
        stop_pct=stop_pct,
        take_pct=take_pct,
        conviction=float(meta.get("conviction") or 0),
        margin_usdt=float(reopened.get("margin_usdt") or 0),
        contracts=float(reopened.get("contracts") or contracts),
    )
    log.warning(
        "CROSS MIGRATE OK %s %s cross margin contracts=%s",
        short,
        side,
        reopened.get("contracts"),
    )
    return True


class CrossMarginAutoMigrator:
    """Periodic steward hook: migrate at most one isolated position per pass."""

    def __init__(self, state_dir: Path, settings: Settings) -> None:
        self.settings = settings
        self.path = state_dir / STATE_FILE
        self._in_progress = False

    def enabled(self) -> bool:
        if not getattr(self.settings, "auto_cross_margin_migrate", True):
            return False
        if not is_cross_margin(self.settings.margin_mode):
            return False
        if self.settings.dry_run:
            return False
        if self.settings.mode != "live":
            return False
        return True

    def _interval(self) -> float:
        return float(
            getattr(self.settings, "auto_cross_margin_interval_sec", DEFAULT_INTERVAL_SEC)
            or DEFAULT_INTERVAL_SEC
        )

    def _symbol_cooldown(self) -> float:
        return float(
            getattr(self.settings, "auto_cross_margin_symbol_cooldown_sec", DEFAULT_SYMBOL_COOLDOWN_SEC)
            or DEFAULT_SYMBOL_COOLDOWN_SEC
        )

    def _symbol_blocked(self, state: dict[str, Any], symbol: str, now: float) -> bool:
        failures = state.get("failures") or {}
        ts = float(failures.get(symbol) or 0)
        return ts > 0 and (now - ts) < self._symbol_cooldown()

    def run(
        self,
        ex: BlofinExchange,
        registry: PositionRegistry,
        positions: dict[str, dict[str, Any]] | None = None,
        *,
        max_per_pass: int = 1,
        force: bool = False,
    ) -> int:
        """
        Migrate up to max_per_pass isolated positions to cross.
        Returns count migrated this pass.
        """
        if not self.enabled() or max_per_pass <= 0:
            return 0
        if self._in_progress:
            return 0

        import api_backoff

        if api_backoff.is_paused():
            return 0

        now = time.time()
        state = _read_state(self.path)
        last_run = float(state.get("last_run") or 0)
        if not force and now - last_run < self._interval():
            return 0

        isolated = list_isolated_positions(ex, positions)
        if not isolated:
            if state.get("last_isolated_count"):
                state["last_isolated_count"] = 0
                state["last_run"] = now
                _write_state(self.path, state)
            return 0

        self._in_progress = True
        migrated = 0
        try:
            ex.ensure_account_margin_mode()
            for _key, pos, mm in isolated[:max_per_pass]:
                symbol = str(pos.get("symbol") or _key.split("#")[0])
                if self._symbol_blocked(state, symbol, now):
                    log.debug(
                        "cross auto-migrate skip %s (cooldown after prior failure)",
                        symbol.split("/")[0],
                    )
                    continue
                ok = migrate_position_to_cross(
                    ex, self.settings, registry, pos, dry_run=False
                )
                state.setdefault("failures", {})
                if ok:
                    migrated += 1
                    state["failures"].pop(symbol, None)
                    state["last_success"] = now
                    state["last_symbol"] = symbol
                else:
                    state["failures"][symbol] = now
                state["last_run"] = now
                state["last_isolated_count"] = len(isolated)
                _write_state(self.path, state)
                if migrated >= max_per_pass:
                    break
        finally:
            self._in_progress = False

        if migrated:
            left = list_isolated_positions(ex)
            if left:
                log.warning(
                    "cross auto-migrate: %d migrated, %d isolated remain",
                    migrated,
                    len(left),
                )
            else:
                log.info("cross auto-migrate: all positions now cross margin")
        return migrated
