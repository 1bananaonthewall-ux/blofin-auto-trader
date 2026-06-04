from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import api_backoff
from api_backoff import RateLimitPaused
from blofin_http import BlofinHttp
from config import Settings
from liquidation_guard import (
    effective_leverage,
    extra_margin_usdt_for_rate,
    margin_rate,
    mission_safe_leverage,
    sl_is_safe,
    sl_tp_from_exchange_liq,
    trigger_prices,
)
from tpsl_guard import (
    ADEQUATE_TOL_PCT,
    PendingTpsl,
    adjust_triggers_for_market,
    extract_pending_tpsl,
    pending_from_registry_prices,
    pct_from_prices,
    pending_is_adequate,
    pending_matches_targets,
)
from leverage_intel import LeverageIntel, leverage_needs_reentry
from margin_mode import is_cross_margin, normalize_margin_mode
from markets import Market, symbol_to_inst_id
from scalp_profile import profile_for

if TYPE_CHECKING:
    from market_stream import BlofinMarketStream

log = logging.getLogger(__name__)


def _open_reject_is_expected(exc: BaseException) -> bool:
    """Known exchange rejects — bot.py cooldowns handle these; not operator errors."""
    msg = str(exc).lower()
    return any(
        code in msg
        for code in ("102115", "102135", "102087")
    ) or any(
        phrase in msg
        for phrase in (
            "delisted",
            "will be delisted",
            "market is closed",
            "maximum available position amount",
        )
    )


def _quantize_order_size(contracts: float, lot_size: float) -> str:
    """Round to exchange lot step and emit a clean size string (avoids 152002 float noise)."""
    step = lot_size if lot_size > 0 else 0.01
    units = max(1, round(float(contracts) / step))
    qty = max(step, units * step)
    step_s = f"{step:.12f}".rstrip("0").rstrip(".")
    if "." in step_s:
        prec = len(step_s.split(".", 1)[1])
        text = f"{qty:.{prec}f}"
    else:
        text = str(int(round(qty)))
    return text.rstrip("0").rstrip(".") if "." in text else text


# How close (as multiplier of liquidation distance) we allow price
# to get before proactively exiting.  E.g. 0.5 means if price is within
# 50% of the gap between entry and liquidation, we exit early.
PRE_LIQUIDATION_EXIT_FACTOR = 0.65

MARKETS_CACHE_FILE = "markets_cache.json"
HEDGE_MODE_CACHE_FILE = "hedge_mode.json"
MARGIN_MODE_CACHE_FILE = "margin_mode.json"
MIN_TPSL_REPAIR_SEC = 15.0
TPSL_VERIFIED_TTL_SEC = 600.0
_TPSL_PRICE_FAULT_CODES = ("103005", "102132", "102134")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _seed_equity_from_state(state_dir: Path) -> tuple[float, float]:
    """Last non-zero equity/free from persisted bot state."""
    snap = _read_json(state_dir / "account_snapshot.json")
    eq = float(snap.get("equity") or 0)
    free = float(snap.get("free_margin") or 0)
    if eq > 0:
        return eq, free if free > 0 else eq

    fluid = _read_json(state_dir / "fluid_state.json")
    samples = fluid.get("samples") or []
    for item in reversed(samples):
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                val = float(item[1])
            else:
                val = float(item)
        except (TypeError, ValueError, IndexError):
            continue
        if val > 0:
            return val, val * 0.85
    peak = float(fluid.get("peak_equity") or 0)
    if peak > 0:
        return peak, peak * 0.85
    return 0.0, 0.0


def _tpsl_profile_kwargs(
    settings: Settings,
    *,
    registry_meta: dict[str, Any] | None = None,
    decision: Any | None = None,
    leverage: int | None = None,
) -> dict[str, float | bool]:
    from tpsl_policy import resolve_tpsl_policy

    policy = resolve_tpsl_policy(
        settings,
        decision=decision,
        registry_meta=registry_meta,
        leverage=leverage,
    )
    return policy.sl_tp_kwargs()


class BlofinExchange:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = BlofinHttp(
            settings.api_key,
            settings.secret,
            settings.passphrase,
            demo=settings.mode == "demo",
        )
        self._hedge_mode = self._load_hedge_mode_cache(default=True)
        self._min_tpsl_repair_sec = MIN_TPSL_REPAIR_SEC
        self.markets: dict[str, Market] = {}
        self._scan_offset = 0
        self._last_api_call = 0.0
        self._min_api_gap = 0.35  # reduce REST burst pressure vs Blofin WAF
        # Cache for VWAP calculation
        self._vwap_cache: dict[str, dict] = {}
        self.stream: BlofinMarketStream | None = None
        self.last_open_error: str = ""
        self.last_repaired_tpsl: tuple[float, float] | None = None
        self.last_repaired_tpsl_prices: tuple[float, float] | None = None
        self._tpsl_repair_at: dict[str, float] = {}
        self._tpsl_verified_at: dict[str, float] = {}
        self._tpsl_verified_prices: dict[str, tuple[float, float]] = {}
        self.leverage_intel = LeverageIntel(settings.state_dir)
        self._cached_positions: dict[str, dict[str, Any]] = {}
        self._cached_equity: float = 0.0
        self._cached_free: float = 0.0
        self._last_equity_ok: bool = True
        self._last_positions_ok: bool = True
        self._margin_top_up_supported: bool | None = None
        self._account_margin_mode = normalize_margin_mode(settings.margin_mode)
        eq, free = _seed_equity_from_state(settings.state_dir)
        if eq > 0:
            self._cached_equity = eq
            self._cached_free = free if free > 0 else eq
            log.info("seeded equity cache from state: $%.4f free=$%.4f", eq, self._cached_free)

    @property
    def equity_fetch_ok(self) -> bool:
        return self._last_equity_ok

    @property
    def positions_fetch_ok(self) -> bool:
        return self._last_positions_ok

    def attach_stream(self, stream: BlofinMarketStream) -> None:
        self.stream = stream

    def _throttle(self):
        """Ensure minimum gap between API calls to avoid rate limiting."""
        gap = 2.0 if api_backoff.is_paused() else self._min_api_gap
        elapsed = time.time() - self._last_api_call
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_api_call = time.time()

    def _safe_request(self, method, *args, retries=3, **kwargs):
        """Make an API request with retry on failure."""
        if api_backoff.is_paused():
            return None

        last_error = None
        for attempt in range(retries):
            if api_backoff.is_paused():
                return None
            self._throttle()
            try:
                result = method(*args, **kwargs)
                if result is not None:
                    return result
            except RateLimitPaused as e:
                log.debug("API paused (no retry): %s", e)
                return None
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                if isinstance(e, RateLimitPaused) or "429" in msg or "rate limit" in msg:
                    log.debug("API rate limited (no retry): %s", e)
                    return None
                log.debug("API call failed (attempt %d/%d): %s", attempt + 1, retries, e)
                if "Too Many Requests" in msg:
                    return None
                time.sleep(1.0 * (attempt + 1))
        log.warning("API call failed after %d retries: %s", retries, last_error)
        return None

    @staticmethod
    def load_markets_from_cache(state_dir: Path) -> dict[str, Market]:
        raw = _read_json(state_dir / MARKETS_CACHE_FILE)
        rows = raw.get("markets") or []
        out: dict[str, Market] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                m = Market(
                    inst_id=str(row["inst_id"]),
                    symbol=str(row["symbol"]),
                    min_size=float(row["min_size"]),
                    contract_size=float(row["contract_size"]),
                    last_price=float(row.get("last_price") or 0),
                    max_leverage=int(row.get("max_leverage") or 50),
                )
                if m.last_price > 0:
                    out[m.symbol] = m
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def save_markets_cache(self, state_dir: Path) -> None:
        if not self.markets:
            return
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": time.time(),
            "markets": [
                {
                    "inst_id": m.inst_id,
                    "symbol": m.symbol,
                    "min_size": m.min_size,
                    "contract_size": m.contract_size,
                    "last_price": m.last_price,
                    "max_leverage": m.max_leverage,
                }
                for m in self.markets.values()
            ],
        }
        path = state_dir / MARKETS_CACHE_FILE
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            log.debug("markets cache write failed", exc_info=True)

    def _load_hedge_mode_cache(self, *, default: bool = True) -> bool:
        raw = _read_json(self.settings.state_dir / HEDGE_MODE_CACHE_FILE)
        if "hedge_mode" in raw:
            return bool(raw["hedge_mode"])
        return default

    def _save_hedge_mode_cache(self) -> None:
        path = self.settings.state_dir / HEDGE_MODE_CACHE_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"hedge_mode": self._hedge_mode, "saved_at": time.time()}),
                encoding="utf-8",
            )
        except Exception:
            log.debug("hedge_mode cache write failed", exc_info=True)

    def _maybe_infer_hedge_mode(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            ps = (row.get("positionSide") or row.get("posSide") or "").lower()
            if ps in ("long", "short"):
                if not self._hedge_mode:
                    self._hedge_mode = True
                    self._save_hedge_mode_cache()
                    log.info("hedge_mode=True (inferred from posSide=%s)", ps)
                return

    def load(self) -> None:
        if api_backoff.is_paused():
            log.warning(
                "API paused (%.0fs left) — skipping position-mode/instruments; loading markets cache",
                api_backoff.seconds_left(),
            )
            self._hedge_mode = self._load_hedge_mode_cache(default=self._hedge_mode)
            cached = self.load_markets_from_cache(self.settings.state_dir)
            if cached:
                self.markets = cached
                log.info("loaded %d markets from cache", len(cached))
            log.info("hedge_mode=%s (cached)", self._hedge_mode)
            return
        mode = self._safe_request(lambda: self.http.request("GET", "/api/v1/account/position-mode"))
        if isinstance(mode, dict) and mode.get("positionMode"):
            self._hedge_mode = mode.get("positionMode") == "long_short_mode"
            self._save_hedge_mode_cache()
        else:
            self._hedge_mode = self._load_hedge_mode_cache(default=self._hedge_mode)
            log.warning("position-mode unavailable — using cached hedge_mode=%s", self._hedge_mode)
        log.info("hedge_mode=%s", self._hedge_mode)
        inst = self.list_instruments()
        n = self.leverage_intel.ingest_instruments(inst)
        if n:
            log.info("leverage intel: cached max leverage for %d instruments", n)

    def list_instruments(self) -> list[dict[str, Any]]:
        """Safely list all instruments with retry logic."""
        return self._safe_request(self.http.list_instruments) or []

    def symbol_leverage_cap(self, symbol: str) -> int:
        """Mission target capped by exchange max for this symbol."""
        desired = int(self.settings.scalp_leverage_max if self.settings.scalp_3r_mode else self.settings.leverage)
        return self.leverage_intel.resolve_target(symbol, desired)

    def list_tickers(self) -> list[dict[str, Any]]:
        """Safely list all tickers with retry logic."""
        return self._safe_request(self.http.list_tickers) or []

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[list[float]]:
        """OHLCV from WebSocket cache when fresh, else REST."""
        if self.stream:
            cached = self.stream.get_ohlcv(symbol, timeframe, min_bars=min(40, limit // 2))
            if cached and len(cached) >= min(40, limit // 2):
                return cached[-limit:]
            self.stream.bootstrap_candles(symbol, bar=timeframe, limit=limit)
            cached = self.stream.get_ohlcv(symbol, timeframe, min_bars=30)
            if cached:
                return cached[-limit:]

        inst_id = symbol_to_inst_id(symbol)
        raw = self._safe_request(lambda: self.http.get_candles(inst_id, bar=timeframe, limit=limit))
        if not raw:
            return []
        result: list[list[float]] = []
        for row in raw:
            if len(row) < 5:
                continue
            try:
                result.append([
                    float(row[0]),  # timestamp (ms)
                    float(row[1]),  # open
                    float(row[2]),  # high
                    float(row[3]),  # low
                    float(row[4]),  # close
                    float(row[5]) if len(row) > 5 else 0.0,  # volume
                ])
            except (TypeError, ValueError):
                log.debug("skipping malformed OHLCV row for %s: %s", symbol, row)
                continue
        return result

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """Fetch the current funding rate for a symbol."""
        inst_id = symbol_to_inst_id(symbol)
        return self._safe_request(lambda: self.http.get_funding_rate(inst_id))

    def refresh_markets(self, markets: list[Market]) -> None:
        self.markets = {m.symbol: m for m in markets}
        for m in markets:
            self.leverage_intel._max_by_inst[m.inst_id] = m.max_leverage
        self.patch_prices_from_stream()

    def patch_prices_from_stream(self) -> int:
        """Update cached market last_price from WS/REST hub (avoids per-symbol REST)."""
        if not self.stream or not self.markets:
            return 0
        n = 0
        patched: dict[str, Market] = {}
        for sym, mkt in self.markets.items():
            px = self.stream.get_last_price(sym)
            if px and px > 0:
                patched[sym] = replace(mkt, last_price=px)
                n += 1
            else:
                patched[sym] = mkt
        if n:
            self.markets = patched
        return n

    def market_for(self, symbol: str) -> Market | None:
        return self.markets.get(symbol)

    def next_scan_batch(self, symbols: list[str], batch_size: int) -> list[str]:
        if not symbols:
            return []
        batch_size = max(1, min(batch_size, len(symbols)))
        start = self._scan_offset % len(symbols)
        batch = symbols[start : start + batch_size]
        if len(batch) < batch_size:
            batch.extend(symbols[: batch_size - len(batch)])
        self._scan_offset = (start + batch_size) % len(symbols)
        return batch

    def fetch_free_equity_usdt(self) -> float:
        try:
            bal = self._safe_request(self.http.get_balance)
            if bal is None:
                self._last_equity_ok = False
                return self._cached_free or self._cached_equity
            self._last_equity_ok = True
            details = bal.get('details', []) if isinstance(bal, dict) else bal
            if isinstance(bal, dict):
                for row in details:
                    if row.get('currency') == 'USDT':
                        self._cached_free = float(row.get('availableEquity', 0.0))
                        return self._cached_free
            self._cached_free = self.fetch_equity_usdt()
            return self._cached_free
        except Exception:
            log.warning("failed to fetch free equity, using cached")
            return self._cached_free or self._cached_equity

    def fetch_equity_usdt(self) -> float:
        data = self._safe_request(self.http.get_balance)
        if data is None:
            self._last_equity_ok = False
            return self._cached_equity
        self._last_equity_ok = True
        if isinstance(data, dict):
            total = data.get("totalEquity") or data.get("equity")
            if total is not None:
                self._cached_equity = float(total)
                return self._cached_equity
        if isinstance(data, list):
            for row in data:
                if row.get("currency") in ("USDT", None) or row.get("ccy") == "USDT":
                    self._cached_equity = float(row.get("equity") or row.get("available") or 0)
                    return self._cached_equity
        return self._cached_equity

    def fetch_all_positions(self) -> dict[str, dict[str, Any]]:
        rows = self._safe_request(self.http.get_positions)
        if rows is None:
            self._last_positions_ok = False
            log.debug("positions fetch failed — using cache (%d)", len(self._cached_positions))
            return dict(self._cached_positions)
        self._last_positions_ok = True
        if isinstance(rows, list):
            self._maybe_infer_hedge_mode(rows)
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            parsed = self._parse_position_row(row)
            if parsed is None:
                continue
            key, pos = parsed
            symbol = pos["symbol"]
            side = pos["side"]
            slot = symbol
            if slot in out:
                prev = out.pop(slot)
                prev_side = prev.get("side", "long")
                prev_key = f"{symbol}#{prev_side}"
                prev["position_key"] = prev_key
                out[prev_key] = prev
                slot = f"{symbol}#{side}"
            pos["position_key"] = slot
            out[slot] = pos
        self._cached_positions = out
        return out

    @staticmethod
    def _float_row_field(row: dict[str, Any], *keys: str) -> float:
        for key in keys:
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _parse_position_row(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        inst_id = row.get("instId") or ""
        if not inst_id:
            return None
        size = self._float_row_field(row, "positions", "pos", "size", "position", "availPos")
        if abs(size) <= 0:
            return None
        symbol = f"{inst_id.replace('-USDT', '')}/USDT:USDT"
        side = (
            row.get("positionSide")
            or row.get("posSide")
            or row.get("side")
            or ""
        ).lower()
        if side in ("", "net"):
            side = "long" if size > 0 else "short"
        entry = self._float_row_field(row, "avgPx", "avgPrice", "averagePrice")
        mark = self._float_row_field(row, "markPrice") or entry
        margin = self._float_row_field(
            row, "margin", "initialMargin", "imr", "isolatedMargin", "marginBal"
        )
        liq = self._float_row_field(row, "liquidationPrice")
        lev = int(self._float_row_field(row, "leverage") or self.settings.leverage or 10)
        mkt = self.market_for(symbol)
        cs = mkt.contract_size if mkt else 1.0
        contracts = abs(size)
        unrealized = self._unrealized_usd_from_row(row)
        roe_pct, pnl_usd, notional, eff_lev = self.position_display_metrics(
            side=side,
            entry=entry,
            mark=mark,
            margin_usdt=margin,
            leverage=lev,
            unrealized_usd=unrealized,
            row=row,
            contracts=contracts,
            contract_size=cs,
        )
        price_move = BlofinExchange._gross_pnl_pct(side, entry, mark)
        pos_side = side if side in ("long", "short") else "net"
        raw_ps = (row.get("positionSide") or row.get("posSide") or "").lower()
        if raw_ps in ("long", "short"):
            pos_side = raw_ps
        pos = {
            "symbol": symbol,
            "contracts": contracts,
            "side": side,
            "pos_side": pos_side,
            "entry_price": entry,
            "mark_price": mark,
            "margin_usdt": margin,
            "liquidation_price": liq,
            "leverage": lev,
            "notional_usdt": notional,
            "margin_rate": margin_rate(notional, margin, lev) if margin > 0 else 0.0,
            "effective_leverage": eff_lev,
            "price_move_pct": round(price_move * 100.0, 4),
            "roe_pct": roe_pct,
            "unrealized_pnl_usd": pnl_usd,
            "info": row,
        }
        return symbol, pos

    def fetch_positions(self, symbol: str) -> list[dict[str, Any]]:
        pos = self.fetch_all_positions().get(symbol)
        return [pos] if pos else []

    def fetch_position_entry_price(self, symbol: str) -> float | None:
        """Fetch the actual average entry price of an open position from the exchange."""
        try:
            inst_id = symbol_to_inst_id(symbol)
            rows = self._safe_request(lambda: self.http.get_positions(inst_id))
            if not rows:
                return None
            row = rows[0] if isinstance(rows, list) else rows
            avg_px = float(row.get("avgPx") or row.get("avgPrice") or 0)
            return avg_px if avg_px > 0 else None
        except Exception:
            log.debug("could not fetch entry price for %s", symbol)
            return None

    def _liquidation_distance_pct(self, leverage: int) -> float:
        """Estimate the liquidation distance as a percentage from entry price.
        
        For isolated margin on perp swaps, liquidation is roughly at:
          Long:  entry * (1 - 1/leverage - maintenance_margin)
          Short: entry * (1 + 1/leverage + maintenance_margin)
        
        Maintenance margin on Blofin is typically ~0.5% for most pairs.
        Returns the one-sided distance as a decimal (e.g. 0.02 = 2%).
        """
        if leverage <= 0:
            return 1.0
        maint_margin = 0.005  # 0.5% maintenance margin for isolated
        distance = 1.0 / leverage + maint_margin
        return distance

    def _position_side_for_order(self, side: str, pos: dict[str, Any] | None = None) -> str:
        if pos:
            ps = str(pos.get("pos_side") or pos.get("positionSide") or "").lower()
            if ps in ("long", "short"):
                return ps
        if self._hedge_mode:
            return "long" if side == "long" else "short"
        return "net"

    def _default_margin_mode(self) -> str:
        return self._account_margin_mode

    def _margin_mode_for_position(self, pos: dict[str, Any] | None) -> str:
        if not pos:
            return self._default_margin_mode()
        row = pos.get("info") if isinstance(pos.get("info"), dict) else pos
        mm = normalize_margin_mode(row.get("marginMode") or row.get("mgnMode") or "")
        return mm if mm else self._default_margin_mode()

    def _save_margin_mode_cache(self, mode: str) -> None:
        path = self.settings.state_dir / MARGIN_MODE_CACHE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"margin_mode": normalize_margin_mode(mode), "saved_at": time.time()},
                indent=2,
            ),
            encoding="utf-8",
        )

    def ensure_account_margin_mode(self) -> str:
        """Sync Blofin account margin mode with settings (cross vs isolated)."""
        want = normalize_margin_mode(self.settings.margin_mode)
        self._account_margin_mode = want
        if self.settings.dry_run:
            log.warning("DRY_RUN: would set account margin mode to %s", want)
            return want
        try:
            cur = self.http.get_margin_mode()
            got = normalize_margin_mode(cur.get("marginMode") or "")
            if got == want:
                log.info("margin mode: %s (account synced)", want)
            else:
                self.http.set_margin_mode(want)
                log.warning("MARGIN MODE switched %s -> %s on exchange", got or "?", want)
        except Exception as exc:
            log.error("set margin mode %s failed: %s", want, exc)
        self.leverage_intel.invalidate_leverage_cache()
        self._save_margin_mode_cache(want)
        return want

    def _ensure_cross_leverage_for_symbol(
        self, symbol: str, position_side: str, leverage: int
    ) -> int:
        """Set leverage for the configured margin mode (cross vs isolated)."""
        applied = self.ensure_leverage(symbol, position_side, leverage)
        want = self._default_margin_mode()
        if not is_cross_margin(want) or applied <= 0:
            return applied
        inst_id = symbol_to_inst_id(symbol)
        side = position_side if self._hedge_mode else "net"
        try:
            info = self.http.get_leverage_info(
                inst_id, margin_mode="cross", position_side=side
            )
            got = normalize_margin_mode(info.get("marginMode") or "")
            got_lev = int(float(info.get("leverage") or 0))
            if got == "cross" and got_lev >= applied:
                return applied
            log.warning(
                "%s cross leverage not synced (api %s %dx) — re-setting",
                symbol.split("/")[0],
                got or "?",
                got_lev,
            )
            self.leverage_intel.invalidate_leverage_cache(inst_id)
            return self.ensure_leverage(symbol, position_side, leverage)
        except Exception as exc:
            log.debug("leverage-info %s: %s", symbol.split("/")[0], exc)
            return applied

    def _format_price(self, price: float) -> str:
        return f"{price:.8f}".rstrip("0").rstrip(".")

    def ensure_margin_cushion(
        self,
        symbol: str,
        side: str,
        *,
        target_margin_rate: float | None = None,
        dry_run: bool = False,
    ) -> bool:
        """
        Add isolated margin on the exchange when collateral is below target_margin_rate.
        No-op in cross margin (shared account collateral).
        """
        if is_cross_margin(self.settings.margin_mode):
            return False
        target = float(
            target_margin_rate
            or getattr(self.settings, "target_margin_rate", 1.15)
            or 1.15
        )
        min_rate = float(getattr(self.settings, "min_margin_rate", 1.08) or 1.08)
        target = max(min_rate, min(target, 1.35))

        if self._margin_top_up_supported is False:
            return False
        if not self.settings.margin_top_up_enabled:
            return False

        pos = self._lookup_open_position(symbol, side)
        if not pos:
            return False
        margin = float(pos.get("margin_usdt") or 0)
        notional = float(pos.get("notional_usdt") or 0)
        lev = int(pos.get("leverage") or self.settings.leverage or 10)
        if margin <= 0 or notional <= 0:
            return False

        mrate = float(pos.get("margin_rate") or margin_rate(notional, margin, lev))
        if mrate >= target - 0.02:
            return True

        add_usdt = extra_margin_usdt_for_rate(notional, lev, target)
        if add_usdt < 0.05:
            return True

        free = self.fetch_free_equity_usdt()
        reserve = max(2.0, float(getattr(self.settings, "margin_reserve_usdt", 0) or 0))
        if free < add_usdt + reserve:
            log.warning(
                "margin cushion %s: need $%.2f free have $%.2f (rate %.0f%% → %.0f%%)",
                symbol.split("/")[0],
                add_usdt,
                free,
                mrate * 100,
                target * 100,
            )
            return False

        if dry_run:
            log.info(
                "DRY_RUN margin +$%.2f %s (rate %.0f%% → target %.0f%%)",
                add_usdt,
                symbol.split("/")[0],
                mrate * 100,
                target * 100,
            )
            return True

        inst_id = symbol_to_inst_id(symbol)
        pos_side = str(pos.get("pos_side") or pos.get("positionSide") or "net").lower()
        if pos_side not in ("long", "short", "net"):
            pos_side = "net"
        margin_mode = self._margin_mode_for_position(pos)
        try:
            self.http.adjust_position_margin(
                inst_id,
                position_side=pos_side,
                margin_mode=margin_mode,
                amount_usdt=add_usdt,
                add=True,
            )
            time.sleep(0.25)
            refreshed = self._lookup_open_position(symbol, side)
            if refreshed:
                nm = float(refreshed.get("margin_usdt") or 0)
                nn = float(refreshed.get("notional_usdt") or notional)
                nl = int(refreshed.get("leverage") or lev)
                new_rate = float(refreshed.get("margin_rate") or margin_rate(nn, nm, nl))
                log.info(
                    "margin cushion %s +$%.2f → rate %.0f%% (liq farther) margin=$%.2f",
                    symbol.split("/")[0],
                    add_usdt,
                    new_rate * 100,
                    nm,
                )
                return new_rate >= min_rate - 0.03
            log.info("margin cushion %s +$%.2f submitted", symbol.split("/")[0], add_usdt)
            return True
        except Exception as e:
            err = str(e)
            if "152404" in err:
                if self._margin_top_up_supported is not False:
                    log.info(
                        "Blofin margin top-up unavailable (152404) — "
                        "anti-liquidation uses 32x sizing + SL buffer only"
                    )
                self._margin_top_up_supported = False
                return False
            log.warning(
                "margin cushion failed %s +$%.2f: %s (sizing uses lower lev / smaller size)",
                symbol.split("/")[0],
                add_usdt,
                err,
            )
            return False

    def open_position(
        self,
        symbol: str,
        side: str,
        contracts: float,
        stop_pct: float,
        take_pct: float,
        dry_run: bool,
        leverage: int | None = None,
    ) -> dict[str, Any] | None:
        """Market entry with SL/TP attached on the same order (exchange-managed exits)."""
        market = self.market_for(symbol)
        inst_id = symbol_to_inst_id(symbol)
        min_size = market.min_size if market else 0.01
        size_str = _quantize_order_size(contracts, min_size)
        size_f = float(size_str)

        price = 0.0
        if self.stream:
            price = self.stream.get_last_price(symbol) or 0.0
        if price <= 0:
            self._throttle()
            ticker = self.http.get_ticker(inst_id)
            price = float(ticker.get("last") or ticker.get("lastPrice") or 0)
        order_side = "buy" if side == "long" else "sell"
        position_side = self._position_side_for_order(side)

        lev = leverage if leverage else self.settings.leverage
        rr_kw = _tpsl_profile_kwargs(self.settings)
        sl_trig, tp_trig, stop_pct, take_pct = trigger_prices(
            side, price, stop_pct, take_pct, lev, min_rr=float(rr_kw["min_rr"])
        )

        pos_pre = self._lookup_open_position(symbol, side)
        margin_mode = self._default_margin_mode()
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "side": order_side,
            "orderType": "market",
            "size": size_str,
            "brokerId": self.settings.broker_id,
        }

        log.info(
            "OPEN %s %s size=%s @~%.4f lev=%dx marginMode=%s (TPSL after fill) dry=%s",
            order_side,
            inst_id,
            size_str,
            price,
            lev,
            margin_mode,
            dry_run,
        )
        if dry_run:
            return None

        self.ensure_account_margin_mode()
        self._ensure_cross_leverage_for_symbol(symbol, position_side, lev)
        time.sleep(0.12)
        try:
            result = self.http.place_order(body)
            time.sleep(0.35)
            pos_after = self._lookup_open_position(symbol, side)
            if pos_after:
                got_mm = self._margin_mode_for_position(pos_after)
                if got_mm != margin_mode:
                    log.error(
                        "OPEN %s filled as %s margin (wanted %s) — close and fix leverage",
                        inst_id,
                        got_mm,
                        margin_mode,
                    )
            ok, rep_stop, rep_take = self.repair_position_tpsl(
                symbol,
                side,
                size_f,
                take_pct=take_pct,
                configured_leverage=lev,
                dry_run=dry_run,
            )
            if ok and rep_stop > 0 and rep_take > 0:
                self.last_repaired_tpsl = (rep_stop, rep_take)
            self.ensure_margin_cushion(symbol, side, dry_run=dry_run)
            return result
        except Exception as e:
            self.last_open_error = str(e)
            if _open_reject_is_expected(e):
                log.warning("open rejected %s: %s", symbol, e)
            else:
                log.error("open failed %s: %s", symbol, e)
            return None

    @staticmethod
    def _is_tpsl_price_fault(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(
            code in msg
            for code in _TPSL_PRICE_FAULT_CODES
        ) or "latest trading price not found" in msg or "mark price not found" in msg

    def _prime_market_price(self, inst_id: str) -> None:
        """Warm Blofin price caches before order-tpsl (reduces 103005/102132 rejections)."""
        try:
            self.http.get_mark_price(inst_id)
        except Exception:
            pass
        try:
            self.http.get_ticker(inst_id)
        except Exception:
            pass

    def _mark_for_symbol(
        self, symbol: str, pos: dict | None = None, *, retries: int = 3
    ) -> float:
        """Resolve last/mark for TPSL — Blofin rejects orders without a live price (103005)."""
        base = self._canonical_symbol(symbol)
        inst_id = symbol_to_inst_id(base)
        entry = float((pos or {}).get("entry_price") or 0)
        info = (pos or {}).get("info") or {}

        for attempt in range(max(1, retries)):
            if self.stream:
                if attempt == 1:
                    try:
                        self.stream.refresh_all_tickers(force=True)
                    except Exception:
                        pass
                px = self.stream.get_last_price(base)
                if px and px > 0:
                    return float(px)
                row = self.stream.get_ticker(base)
                if row:
                    for key in ("last", "lastPrice", "markPrice", "markPx"):
                        try:
                            v = float(row.get(key) or 0)
                        except (TypeError, ValueError):
                            continue
                        if v > 0:
                            return v
            if pos:
                mark = float(pos.get("mark_price") or 0)
                if mark > 0:
                    return mark
                for key in ("markPx", "markPrice", "last", "lastPx"):
                    try:
                        v = float(info.get(key) or 0)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        return v
            self._throttle()
            try:
                ticker = self.http.get_ticker(inst_id)
                for key in ("last", "lastPrice", "markPrice", "markPx"):
                    try:
                        v = float(ticker.get(key) or 0)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        return v
            except Exception as exc:
                if "103005" not in str(exc) and attempt >= retries - 1:
                    log.debug("ticker %s failed: %s", inst_id, exc)
            try:
                candles = self.http.get_candles(inst_id, bar="1m", limit=3)
                if candles:
                    close = float(candles[-1][4])
                    if close > 0:
                        return close
            except Exception:
                pass
            if entry > 0 and attempt >= retries - 1:
                return entry
            time.sleep(0.35 * (attempt + 1))
        return 0.0

    def _seed_tpsl_trust_from_registry(self, state_dir: Path) -> None:
        reg = _read_json(state_dir / "position_registry.json")
        now = time.time()
        for sym, row in reg.items():
            if not isinstance(row, dict):
                continue
            sl = float(row.get("sl_price") or 0)
            tp = float(row.get("tp_price") or 0)
            if sl > 0 and tp > 0:
                base = self._canonical_symbol(sym)
                self._tpsl_verified_at[base] = now
                self._tpsl_verified_prices[base] = (sl, tp)
                self._tpsl_repair_at[base] = now

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        base = str(symbol).split("#", 1)[0]
        return base

    def _lookup_open_position(self, symbol: str, side: str | None = None) -> dict[str, Any] | None:
        """Resolve position dict by bare symbol or symbol#side key."""
        base = self._canonical_symbol(symbol)
        positions = self.fetch_all_positions()
        if side:
            keyed = f"{base}#{side.lower()}"
            pos = positions.get(keyed)
            if pos:
                return pos
        pos = positions.get(base)
        if pos:
            return pos
        for key, row in positions.items():
            if self._canonical_symbol(key) == base:
                if side is None or str(row.get("side") or "").lower() == side.lower():
                    return row
        return None

    def _record_tpsl_verified(
        self, symbol: str, sl_price: float, tp_price: float
    ) -> None:
        base = self._canonical_symbol(symbol)
        if sl_price > 0 and tp_price > 0:
            self._tpsl_verified_at[base] = time.time()
            self._tpsl_verified_prices[base] = (sl_price, tp_price)
            self._tpsl_repair_at[base] = time.time()

    def _trusted_pending(
        self,
        symbol: str,
        side: str,
        entry: float,
        *,
        registry_meta: dict[str, Any] | None = None,
    ) -> PendingTpsl | None:
        """Use recent verify cache or registry SL/TP when REST pending is empty."""
        base = self._canonical_symbol(symbol)
        now = time.time()
        verified_at = self._tpsl_verified_at.get(base, 0.0)
        if verified_at > 0 and now - verified_at <= TPSL_VERIFIED_TTL_SEC:
            prices = self._tpsl_verified_prices.get(base)
            if prices:
                pending = pending_from_registry_prices(
                    side, entry, prices[0], prices[1]
                )
                if pending and pending_is_adequate(side, entry, pending):
                    return pending
        meta = registry_meta or {}
        reg_sl = float(meta.get("sl_price") or 0)
        reg_tp = float(meta.get("tp_price") or 0)
        reg_at = float(meta.get("tpsl_verified_at") or meta.get("opened_at") or 0)
        if reg_sl > 0 and reg_tp > 0 and reg_at > 0 and now - reg_at <= TPSL_VERIFIED_TTL_SEC:
            pending = pending_from_registry_prices(side, entry, reg_sl, reg_tp)
            if pending and pending_is_adequate(side, entry, pending):
                return pending
        return None

    def _pending_tpsl(
        self,
        inst_id: str,
        side: str,
        entry: float,
        *,
        position_side: str | None = None,
        registry_meta: dict[str, Any] | None = None,
        retries: int = 2,
        allow_registry_fallback: bool = True,
    ) -> tuple[list[dict], PendingTpsl]:
        rows: list[dict] = []
        pending = extract_pending_tpsl(side, entry, [], position_side=position_side)
        for attempt in range(max(1, retries)):
            rows = self._fetch_pending_tpsl_rows(inst_id, retries=1, pause_sec=0.2)
            pending = extract_pending_tpsl(
                side, entry, rows, position_side=position_side
            )
            if pending.live_rows > 0 or attempt >= retries - 1:
                break
            time.sleep(0.28)
        if pending.live_rows == 0 and allow_registry_fallback:
            symbol_guess = f"{inst_id.replace('-USDT', '')}/USDT:USDT"
            trusted = self._trusted_pending(
                symbol_guess, side, entry, registry_meta=registry_meta
            )
            if trusted:
                return rows, trusted
        return rows, pending

    def _clear_tpsl_trust(self, symbol: str) -> None:
        base = self._canonical_symbol(symbol)
        self._tpsl_verified_at.pop(base, None)
        self._tpsl_verified_prices.pop(base, None)

    def live_exchange_tpsl(
        self,
        symbol: str,
        side: str,
        entry: float,
        *,
        pos: dict[str, Any] | None = None,
    ) -> PendingTpsl | None:
        """True only when Blofin pending API shows both SL+TP live for this position."""
        pos = pos or self._lookup_open_position(symbol, side)
        if not pos or entry <= 0:
            return None
        inst_id = symbol_to_inst_id(self._canonical_symbol(symbol))
        position_side = self._position_side_for_order(side, pos)
        rows = self._fetch_pending_tpsl_rows(inst_id, retries=5, pause_sec=0.3)
        pending = extract_pending_tpsl(
            side, entry, rows, position_side=position_side
        )
        if (
            pending.live_rows > 0
            and pending.has_sl
            and pending.has_tp
            and pending_is_adequate(side, entry, pending)
        ):
            return pending
        return None

    @staticmethod
    def _gross_pnl_pct(side: str, entry: float, mark: float) -> float:
        if entry <= 0 or mark <= 0:
            return 0.0
        if side.lower() == "long":
            return (mark - entry) / entry
        return (entry - mark) / entry

    @staticmethod
    def estimate_realized_pnl_usd(
        *,
        side: str,
        entry: float,
        exit_px: float,
        fill_pnl: float | None = None,
        margin_usdt: float | None = None,
        leverage: int | None = None,
        contracts: float | None = None,
        contract_size: float = 1.0,
        notional_usdt: float | None = None,
        roe_pct: float | None = None,
    ) -> float:
        """
        Realized PnL in USDT — never treat (exit - entry) as dollars (breaks XPD/XPT ~$1300 tickers).
        """
        if fill_pnl is not None and abs(float(fill_pnl)) > 1e-9:
            return round(float(fill_pnl), 6)
        margin = float(margin_usdt or 0)
        lev = int(leverage or 0)
        # Never derive dollars from stored ROE — fallback ROE is often price_move×lev (~200%).
        _ = roe_pct
        if entry <= 0 or exit_px <= 0:
            return 0.0
        gross = BlofinExchange._gross_pnl_pct(side, entry, exit_px)
        notional = float(notional_usdt or 0)
        if notional <= 0 and margin > 0 and lev > 0:
            notional = margin * lev
        if notional <= 0 and contracts and float(contracts) > 0:
            notional = abs(float(contracts)) * float(contract_size) * entry
        if notional > 0:
            return round(notional * gross, 6)
        if margin > 0 and lev > 0:
            return round(margin * gross * lev, 6)
        return 0.0

    @staticmethod
    def _roe_pct_from_row(row: dict[str, Any]) -> float | None:
        """Exchange ROE on margin (matches Blofin UI). Prefer unrealizedPnlRatio."""
        for key in ("unrealizedPnlRatio", "uplRatio", "pnlRatio", "roe"):
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if abs(v) <= 1.5:
                return v * 100.0
            return v
        return None

    @staticmethod
    def position_display_metrics(
        *,
        side: str,
        entry: float,
        mark: float,
        margin_usdt: float,
        leverage: int,
        unrealized_usd: float | None = None,
        row: dict[str, Any] | None = None,
        contracts: float = 0.0,
        contract_size: float = 1.0,
    ) -> tuple[float, float, float, float]:
        """
        Returns (roe_pct, pnl_usd, notional_usdt, effective_leverage).
        ROE = unrealized PnL / margin (Blofin UI style).
        """
        pnl = unrealized_usd
        if pnl is None and row is not None:
            pnl = BlofinExchange._unrealized_usd_from_row(row)
        if margin_usdt > 0 and leverage > 0:
            notional = margin_usdt * leverage
        elif mark > 0:
            notional = abs(contracts) * contract_size * mark
        else:
            notional = 0.0
        roe = BlofinExchange._roe_pct_from_row(row) if row else None
        if roe is None and pnl is not None and margin_usdt > 0:
            roe = (pnl / margin_usdt) * 100.0
        if roe is None and entry > 0 and mark > 0 and leverage > 0:
            move = BlofinExchange._gross_pnl_pct(side, entry, mark)
            roe = move * leverage * 100.0
            if pnl is None and notional > 0:
                pnl = notional * move
        eff = effective_leverage(notional, margin_usdt, leverage) if margin_usdt > 0 else float(leverage)
        return (
            round(roe or 0.0, 2),
            round(pnl or 0.0, 6),
            round(notional, 4),
            float(eff),
        )

    @staticmethod
    def _unrealized_usd_from_row(row: dict[str, Any]) -> float | None:
        for key in ("unrealizedPnl", "unrealizedPnlUsd", "upl", "uplLastPx", "pnl"):
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    def _fetch_pending_tpsl_rows(
        self,
        inst_id: str,
        *,
        retries: int = 4,
        pause_sec: float = 0.28,
    ) -> list[dict]:
        """Fetch live TPSL rows; never treat a single empty response as authoritative."""
        rows: list[dict] = []
        for attempt in range(max(1, retries)):
            try:
                chunk = self.http.get_pending_tpsl(inst_id) or []
            except Exception:
                chunk = []
            if chunk:
                rows = chunk
                break
            if attempt < retries - 1:
                time.sleep(pause_sec)
        if not rows:
            try:
                all_rows = self.http.get_pending_tpsl() or []
                rows = [r for r in all_rows if str(r.get("instId") or "") == inst_id]
            except Exception:
                rows = []
        return rows

    def _cancel_pending_tpsl(self, inst_id: str) -> int:
        """Cancel only when we have confirmed live pending rows (never on empty API)."""
        rows = self._fetch_pending_tpsl_rows(inst_id)
        if not rows:
            log.debug("skip TPSL cancel %s — no live pending rows confirmed", inst_id)
            return 0
        canceled = 0
        for row in rows:
            tid = row.get("tpslId")
            if tid:
                try:
                    self.http.cancel_tpsl(inst_id, str(tid))
                    canceled += 1
                except Exception:
                    pass
        if canceled:
            time.sleep(0.15)
        return canceled

    def _place_tpsl_leg(
        self,
        inst_id: str,
        position_side: str,
        close_side: str,
        *,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        dry_run: bool,
        margin_mode: str | None = None,
        side: str | None = None,
        symbol: str | None = None,
        mark: float = 0.0,
    ) -> bool:
        mm = normalize_margin_mode(margin_mode or self._default_margin_mode())
        body: dict[str, Any] = {
            "instId": inst_id,
            "marginMode": mm,
            "positionSide": position_side,
            "side": close_side,
            "size": "-1",
            "reduceOnly": "true",
            "brokerId": self.settings.broker_id,
        }
        if sl_price > 0:
            body["slTriggerPrice"] = self._format_price(sl_price)
            body["slOrderPrice"] = "-1"
        if tp_price > 0:
            body["tpTriggerPrice"] = self._format_price(tp_price)
            body["tpOrderPrice"] = "-1"
        if sl_price <= 0 and tp_price <= 0:
            return False
        if dry_run:
            return True

        def _submit(sl: float, tp: float, *, trigger_type: str = "last") -> bool:
            self._prime_market_price(inst_id)
            leg_body = dict(body)
            if sl > 0:
                leg_body["slTriggerPrice"] = self._format_price(sl)
                leg_body["slOrderPrice"] = "-1"
                leg_body["slTriggerPriceType"] = trigger_type
            else:
                leg_body.pop("slTriggerPrice", None)
                leg_body.pop("slOrderPrice", None)
                leg_body.pop("slTriggerPriceType", None)
            if tp > 0:
                leg_body["tpTriggerPrice"] = self._format_price(tp)
                leg_body["tpOrderPrice"] = "-1"
                leg_body["tpTriggerPriceType"] = trigger_type
            else:
                leg_body.pop("tpTriggerPrice", None)
                leg_body.pop("tpOrderPrice", None)
                leg_body.pop("tpTriggerPriceType", None)
            if sl <= 0 and tp <= 0:
                return False
            resp = self.http.place_order_tpsl(leg_body)
            inner = resp if isinstance(resp, dict) else {}
            inner_code = str(inner.get("code") or "0")
            if inner_code not in ("0", "200", ""):
                log.warning(
                    "TPSL leg rejected %s code=%s msg=%s sl=%.6f tp=%.6f",
                    inst_id,
                    inner_code,
                    inner.get("msg"),
                    sl,
                    tp,
                )
                return False
            return True

        side_l = (side or "").lower()
        sym = symbol or inst_id.replace("-USDT", "/USDT:USDT")

        def _retry_with_mark(sl: float, tp: float, ref_mark: float, trigger_type: str) -> bool:
            if side_l and ref_mark > 0:
                adj_sl, adj_tp = adjust_triggers_for_market(side_l, sl, tp, ref_mark)
                if sl > 0 and adj_sl <= 0:
                    adj_sl = sl
                if tp > 0 and adj_tp <= 0:
                    adj_tp = tp
                if adj_sl != sl or adj_tp != tp:
                    log.info(
                        "TPSL %s retry vs mark=%.6f sl=%.6f tp=%.6f type=%s",
                        inst_id,
                        ref_mark,
                        adj_sl,
                        adj_tp,
                        trigger_type,
                    )
                    return _submit(adj_sl, adj_tp, trigger_type=trigger_type)
            return False

        try:
            ref = mark if mark > 0 else self._mark_for_symbol(sym, retries=4)
            if ref > 0 and side_l:
                sl_price, tp_price = adjust_triggers_for_market(
                    side_l, sl_price, tp_price, ref
                )
            for trigger_type in ("last", "mark", "index"):
                try:
                    if _submit(sl_price, tp_price, trigger_type=trigger_type):
                        return True
                except Exception as inner:
                    if not self._is_tpsl_price_fault(inner):
                        raise
                    log.debug(
                        "TPSL %s price fault with trigger=%s — trying next",
                        inst_id,
                        trigger_type,
                    )
            return False
        except Exception as exc:
            msg = str(exc)
            msg_l = msg.lower()
            if "102114" in msg_l or "already set" in msg_l:
                log.debug("TPSL %s may already exist — will verify on exchange", inst_id)
                return True
            if self._is_tpsl_price_fault(exc):
                api_backoff.register_short_pause(
                    45.0, source=f"TPSL price feed {inst_id}"
                )
                log.warning(
                    "TPSL %s deferred — exchange price feed missing (103005/102132); "
                    "steward backup + retry in ~45s",
                    inst_id,
                )
                return False
            if side_l and ("102037" in msg or "102038" in msg or "102040" in msg):
                for attempt in range(2):
                    fresh = self._mark_for_symbol(sym, retries=4)
                    if fresh <= 0:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    try:
                        if _retry_with_mark(sl_price, tp_price, fresh, "mark"):
                            return True
                    except Exception as exc2:
                        exc = exc2
                        if self._is_tpsl_price_fault(exc2):
                            break
                    time.sleep(0.45 * (attempt + 1))
            log.warning("TPSL leg failed %s sl=%.6f tp=%.6f: %s", inst_id, sl_price, tp_price, exc)
            return False

    def _verify_tpsl_on_exchange(
        self,
        inst_id: str,
        side: str,
        entry: float,
        target_sl: float,
        target_tp: float,
        *,
        position_side: str | None = None,
    ) -> tuple[bool, PendingTpsl]:
        rows = self._fetch_pending_tpsl_rows(inst_id, retries=4)
        pending = extract_pending_tpsl(
            side, entry, rows, position_side=position_side
        )
        ok, issues = pending_matches_targets(side, entry, pending, target_sl, target_tp)
        if not ok:
            log.warning(
                "TPSL verify %s issues=%s live_sl=%.6f live_tp=%.6f want_sl=%.6f want_tp=%.6f rows=%d",
                inst_id,
                ",".join(issues) or "unknown",
                pending.sl_price,
                pending.tp_price,
                target_sl,
                target_tp,
                pending.live_rows,
            )
        return ok, pending

    def place_full_position_tpsl(
        self,
        symbol: str,
        side: str,
        *,
        sl_price: float,
        tp_price: float,
        dry_run: bool,
        cancel_existing: bool = True,
        max_stop_pct: float | None = None,
        max_take_pct: float | None = None,
    ) -> bool:
        """
        Place exchange TP+SL for the full position (size=-1).
        Uses combined order then separate legs if verification fails.
        """
        base_sym = self._canonical_symbol(symbol)
        pos = self._lookup_open_position(symbol, side)
        inst_id = symbol_to_inst_id(base_sym)
        position_side = self._position_side_for_order(side, pos)
        margin_mode = self._margin_mode_for_position(pos)
        close_side = "sell" if side == "long" else "buy"
        entry = float(pos.get("entry_price") or 0) if pos else 0.0
        mark = self._mark_for_symbol(base_sym, pos, retries=4)
        if mark <= 0:
            log.warning(
                "TPSL %s skipped — no mark price (103005 risk); will retry next pass",
                inst_id,
            )
            return False
        sl_price, tp_price = adjust_triggers_for_market(side, sl_price, tp_price, mark)

        _, pending0 = self._pending_tpsl(
            inst_id,
            side,
            entry,
            position_side=position_side,
            allow_registry_fallback=False,
            retries=3,
        )
        from tpsl_guard import pending_exceeds_policy_caps

        stale_wide = (
            max_stop_pct is not None
            and max_take_pct is not None
            and pending_exceeds_policy_caps(
                side, entry, pending0, max_stop_pct, max_take_pct
            )
        )
        if stale_wide and not dry_run:
            for _ in range(3):
                n = self._cancel_pending_tpsl(inst_id)
                time.sleep(0.25)
                _, pending0 = self._pending_tpsl(
                    inst_id,
                    side,
                    entry,
                    position_side=position_side,
                    allow_registry_fallback=False,
                    retries=3,
                )
                if pending0.live_rows == 0 or n == 0:
                    break
            cancel_existing = False
            stale_wide = (
                max_stop_pct is not None
                and max_take_pct is not None
                and pending_exceeds_policy_caps(
                    side, entry, pending0, max_stop_pct, max_take_pct
                )
            )
        if pending0.live_rows > 0 and pending_is_adequate(side, entry, pending0) and not stale_wide:
            self._record_tpsl_verified(base_sym, pending0.sl_price, pending0.tp_price)
            return True

        need_both = pending0.live_rows == 0 or (
            not pending0.has_sl and not pending0.has_tp
        )
        if cancel_existing and not dry_run and pending0.live_rows > 0 and need_both:
            self._cancel_pending_tpsl(inst_id)
        elif cancel_existing and not dry_run:
            cancel_existing = False

        def _still_wide(pending: PendingTpsl) -> bool:
            return bool(
                max_stop_pct is not None
                and max_take_pct is not None
                and pending_exceeds_policy_caps(
                    side, entry, pending, max_stop_pct, max_take_pct
                )
            )

        ok = True
        replace_all = stale_wide or pending0.live_rows == 0 or (
            not pending0.has_sl and not pending0.has_tp
        )
        leg_kw = dict(
            side=side,
            symbol=base_sym,
            mark=mark,
        )

        def _refresh_mark() -> float:
            m = self._mark_for_symbol(base_sym, pos, retries=4)
            leg_kw["mark"] = m
            return m
        if replace_all:
            ok = self._place_tpsl_leg(
                inst_id,
                position_side,
                close_side,
                sl_price=sl_price,
                tp_price=tp_price,
                dry_run=dry_run,
                margin_mode=margin_mode,
                **leg_kw,
            )
        if not dry_run:
            time.sleep(0.4)
            verified, pending = self._verify_tpsl_on_exchange(
                inst_id, side, entry, sl_price, tp_price, position_side=position_side
            )
            if pending.live_rows > 0 and (verified or pending_is_adequate(side, entry, pending)):
                if not _still_wide(pending):
                    self._record_tpsl_verified(base_sym, pending.sl_price, pending.tp_price)
                    return True
            if api_backoff.is_paused() and not ok:
                log.warning(
                    "TPSL %s verify fail during API pause — skip leg spam",
                    inst_id,
                )
                return False
            if pending.has_sl and not pending.has_tp:
                log.warning("TPSL %s: SL live but TP missing — placing TP leg", inst_id)
            elif pending.has_tp and not pending.has_sl:
                log.warning("TPSL %s: TP live but SL missing — placing SL leg", inst_id)
            else:
                log.warning("TPSL %s: re-placing legs after verify fail", inst_id)
                if pending.live_rows > 0 and not pending.has_sl and not pending.has_tp:
                    self._cancel_pending_tpsl(inst_id)
            mark = _refresh_mark()
            if mark <= 0:
                log.warning("TPSL %s leg retry aborted — mark still unavailable", inst_id)
                return False
            if not pending.has_sl:
                adj_sl, _ = adjust_triggers_for_market(side, sl_price, 0.0, mark)
                ok = self._place_tpsl_leg(
                    inst_id,
                    position_side,
                    close_side,
                    sl_price=adj_sl,
                    dry_run=dry_run,
                    margin_mode=margin_mode,
                    **leg_kw,
                ) and ok
                time.sleep(0.15)
            if not pending.has_tp:
                _, adj_tp = adjust_triggers_for_market(side, 0.0, tp_price, mark)
                ok = self._place_tpsl_leg(
                    inst_id,
                    position_side,
                    close_side,
                    tp_price=adj_tp,
                    dry_run=dry_run,
                    margin_mode=margin_mode,
                    **leg_kw,
                ) and ok
                time.sleep(0.15)
            verified, pending = self._verify_tpsl_on_exchange(
                inst_id, side, entry, sl_price, tp_price, position_side=position_side
            )
            if pending.live_rows > 0 and (verified or pending_is_adequate(side, entry, pending)):
                if not _still_wide(pending):
                    self._record_tpsl_verified(base_sym, pending.sl_price, pending.tp_price)
                    return True
            if pending.live_rows > 0 and pending.has_tp and pending.has_sl:
                if not _still_wide(pending):
                    log.warning(
                        "TPSL %s: both legs live but drift — keeping exchange orders",
                        inst_id,
                    )
                    self._record_tpsl_verified(base_sym, pending.sl_price, pending.tp_price)
                    return True
            log.error(
                "TPSL VERIFY FAILED %s — missing protection; steward may backup-close on breach",
                inst_id,
            )
            return False
        return ok

    def repair_position_tpsl(
        self,
        symbol: str,
        side: str,
        contracts: float,
        *,
        take_pct: float,
        configured_leverage: int,
        dry_run: bool,
        cancel_existing: bool = True,
        registry_meta: dict[str, Any] | None = None,
    ) -> tuple[bool, float, float]:
        """Attach SL/TP using exchange liquidationPrice — fixes under-margined positions."""
        if api_backoff.is_paused():
            log.debug(
                "TPSL repair skipped %s — API paused (%.0fs)",
                symbol.split("/")[0],
                api_backoff.seconds_left(),
            )
            return False, 0.0, take_pct
        base_sym = self._canonical_symbol(symbol)
        inst_id = symbol_to_inst_id(base_sym)
        pos = self._lookup_open_position(symbol, side)
        if not pos:
            log.warning("repair TPSL: no position %s", base_sym)
            return False, 0.0, 0.0

        entry = float(pos.get("entry_price") or 0)
        liq = float(pos.get("liquidation_price") or 0)
        margin = float(pos.get("margin_usdt") or 0)
        eff_lev = int(pos.get("effective_leverage") or configured_leverage)
        mrate = float(pos.get("margin_rate") or 0)

        meta = dict(registry_meta or {})
        if not meta.get("trade_style") and getattr(self.settings, "scalp_fast_3r", False):
            meta.setdefault("trade_style", "fast_3r")
        from tpsl_policy import exchange_tpsl_from_position, use_fixed_lethal_tpsl

        raw_stop = float(meta.get("stop_pct") or 0)
        stop_hint = raw_stop if raw_stop > 0 else None
        sl_trig, tp_trig, stop_pct, take_pct, policy = exchange_tpsl_from_position(
            self.settings,
            side,
            entry,
            liq,
            take_pct,
            eff_lev,
            registry_meta=meta,
            stop_hint=stop_hint,
        )
        rr = take_pct / max(stop_pct, 1e-9)
        fixed_lethal = use_fixed_lethal_tpsl(self.settings, registry_meta=meta)

        if not fixed_lethal:
            from liquidation_guard import achievable_margin_rates

            equity = self.fetch_equity_usdt()
            min_rate, _ = achievable_margin_rates(self.settings, equity)
            if mrate > 0 and mrate < min_rate - 0.03:
                log.warning(
                    "repair TPSL blocked %s: margin rate %.0f%% < %.0f%% — under-margined",
                    symbol.split("/")[0],
                    mrate * 100,
                    min_rate * 100,
                )
                return False, stop_pct, take_pct

            if not sl_is_safe(side, entry, sl_trig, liquidation_price=liq, leverage=eff_lev):
                log.error(
                    "repair TPSL FAILED %s %s entry=%.6f sl=%.6f liq=%.6f margin=$%.3f rate=%.0f%% eff_lev=%dx",
                    symbol,
                    side,
                    entry,
                    sl_trig,
                    liq,
                    margin,
                    mrate * 100,
                    eff_lev,
                )
                return False, stop_pct, take_pct

        position_side = self._position_side_for_order(side, pos)
        margin_mode = self._margin_mode_for_position(pos)
        self._clear_tpsl_trust(base_sym)
        mark = self._mark_for_symbol(base_sym, pos)

        from tpsl_guard import pending_exceeds_policy_caps

        wide = False
        live = self.live_exchange_tpsl(base_sym, side, entry, pos=pos)
        if live:
            stop_pct, take_pct = pct_from_prices(side, entry, live.sl_price, live.tp_price)
            wide = pending_exceeds_policy_caps(
                side, entry, live, policy.max_stop_pct, policy.max_take_pct
            )
            liq_ok = fixed_lethal or sl_is_safe(
                side, entry, live.sl_price, liquidation_price=liq, leverage=eff_lev
            )
            if liq_ok and not wide:
                self._record_tpsl_verified(base_sym, live.sl_price, live.tp_price)
                self.last_repaired_tpsl_prices = (live.sl_price, live.tp_price)
                log.debug(
                    "TPSL ok %s %s %s sl=%.6f tp=%.6f (%.2f%%/%.2f%%)",
                    symbol.split("/")[0],
                    side,
                    policy.style,
                    live.sl_price,
                    live.tp_price,
                    stop_pct * 100,
                    take_pct * 100,
                )
                return True, stop_pct, take_pct
            if wide:
                log.warning(
                    "TPSL retighten %s %s (%s): live %.2f%%/%.2f%% > cap %.2f%%/%.2f%%",
                    symbol.split("/")[0],
                    side,
                    policy.style,
                    stop_pct * 100,
                    take_pct * 100,
                    policy.max_stop_pct * 100,
                    policy.max_take_pct * 100,
                )
                if not dry_run:
                    self._cancel_pending_tpsl(inst_id)

        sl_trig, tp_trig = adjust_triggers_for_market(side, sl_trig, tp_trig, mark)
        stop_pct, take_pct = pct_from_prices(side, entry, sl_trig, tp_trig)
        log.warning(
            "TPSL attach %s %s %s — sl=%.6f tp=%.6f (%.2f%% / %.2f%%) fee-aware lethal",
            symbol.split("/")[0],
            side,
            policy.style,
            sl_trig,
            tp_trig,
            stop_pct * 100,
            take_pct * 100,
        )
        self._tpsl_repair_at.pop(base_sym, None)
        exchange_ok = False
        for attempt in range(3):
            if dry_run:
                exchange_ok = True
                break
            self.place_full_position_tpsl(
                symbol,
                side,
                sl_price=sl_trig,
                tp_price=tp_trig,
                dry_run=False,
                cancel_existing=wide,
                max_stop_pct=policy.max_stop_pct,
                max_take_pct=policy.max_take_pct,
            )
            time.sleep(0.45)
            live = self.live_exchange_tpsl(base_sym, side, entry, pos=pos)
            if live:
                stop_pct, take_pct = pct_from_prices(side, entry, live.sl_price, live.tp_price)
                still_wide = pending_exceeds_policy_caps(
                    side,
                    entry,
                    live,
                    policy.max_stop_pct,
                    policy.max_take_pct,
                )
                if still_wide:
                    log.warning(
                        "TPSL still wide %s %s after attach (%.2f%%/%.2f%%) — retry",
                        symbol.split("/")[0],
                        side,
                        stop_pct * 100,
                        take_pct * 100,
                    )
                    if not dry_run:
                        self._cancel_pending_tpsl(inst_id)
                    continue
                exchange_ok = True
                sl_trig, tp_trig = live.sl_price, live.tp_price
                self.last_repaired_tpsl_prices = (sl_trig, tp_trig)
                self._record_tpsl_verified(base_sym, sl_trig, tp_trig)
                self._tpsl_repair_at[base_sym] = time.time()
                log.info(
                    "TPSL LIVE %s %s sl=%.6f tp=%.6f (%.2f%%/%.2f%% attempt %d)",
                    symbol.split("/")[0],
                    side,
                    sl_trig,
                    tp_trig,
                    stop_pct * 100,
                    take_pct * 100,
                    attempt + 1,
                )
                break
            log.warning(
                "TPSL attach attempt %d/3 failed %s %s — still no pending on exchange",
                attempt + 1,
                symbol.split("/")[0],
                side,
            )
        if not exchange_ok:
            self._clear_tpsl_trust(base_sym)
            log.error(
                "TPSL FAILED %s %s — no exchange TP/SL after 3 attempts (check margin/liq)",
                symbol.split("/")[0],
                side,
            )
            return False, stop_pct, take_pct
        return True, stop_pct, take_pct

    def repair_all_open_tpsl(
        self,
        settings: Any,
        *,
        registry: Any | None = None,
    ) -> int:
        """Attach exchange SL+TP for every open position missing live TPSL rows."""
        from position_registry import PositionRegistry
        from tpsl_guard import pending_exceeds_policy_caps, pending_is_adequate
        from tpsl_policy import resolve_tpsl_policy

        reg = registry or PositionRegistry(settings.state_dir)
        repaired = 0
        for sym, pos in list(self.fetch_all_positions().items()):
            trade = str(pos.get("symbol") or sym).split("#", 1)[0]
            side = str(pos.get("side") or "")
            entry = float(pos.get("entry_price") or 0)
            contracts = float(pos.get("contracts") or 0)
            if not side or entry <= 0 or contracts <= 0:
                continue
            inst = symbol_to_inst_id(trade)
            ps = self._position_side_for_order(side, pos)
            meta = reg.get(trade) or {}
            lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
            policy = resolve_tpsl_policy(settings, registry_meta=meta, leverage=lev)
            _, pending = self._pending_tpsl(
                inst, side, entry, position_side=ps, allow_registry_fallback=False
            )
            if (
                pending.live_rows > 0
                and pending_is_adequate(side, entry, pending)
                and not pending_exceeds_policy_caps(
                    side, entry, pending, policy.max_stop_pct, policy.max_take_pct
                )
            ):
                continue
            self._clear_tpsl_trust(trade)
            self._tpsl_repair_at.pop(self._canonical_symbol(trade), None)
            meta = reg.get(trade) or {}
            take = float(meta.get("take_pct") or pos.get("take_pct") or 0.022)
            lev = int(pos.get("effective_leverage") or settings.scalp_leverage_max)
            ok, _, _ = self.repair_position_tpsl(
                trade,
                side,
                contracts,
                take_pct=take,
                configured_leverage=lev,
                dry_run=settings.dry_run,
                cancel_existing=False,
                registry_meta=meta,
            )
            if ok:
                repaired += 1
            time.sleep(0.25)
        return repaired

    def ensure_position_tpsl(
        self,
        symbol: str,
        side: str,
        contracts: float,
        stop_price: float,
        take_price: float,
        dry_run: bool,
    ) -> None:
        """Ensure TP/SL orders exist for an open position."""
        _ = contracts
        pos = self.fetch_all_positions().get(symbol)
        entry = float(pos.get("entry_price") or 0) if pos else 0.0
        liq = float(pos.get("liquidation_price") or 0) if pos else 0.0
        buf = getattr(self.settings, "sl_liq_buffer", 0.38)

        take_pct_hint = (
            abs(take_price - entry) / entry
            if entry > 0 and take_price > 0
            else (abs(entry - stop_price) / entry if entry > 0 else 0.02)
        )
        if entry > 0:
            from tpsl_policy import exchange_tpsl_from_position

            lev = int(pos.get("effective_leverage") or self.settings.scalp_leverage_max) if pos else self.settings.leverage
            stop_price, take_price, stop_pct, take_pct, _pol = exchange_tpsl_from_position(
                self.settings,
                side,
                entry,
                liq,
                take_pct_hint,
                lev,
            )
            rr = take_pct / max(stop_pct, 1e-9)
        else:
            stop_pct = take_pct = rr = 0.0

        self.place_full_position_tpsl(
            symbol,
            side,
            sl_price=stop_price,
            tp_price=take_price,
            dry_run=dry_run,
            cancel_existing=True,
        )
        if entry > 0:
            log.info(
                "TPSL ensured %s sl=%.4f tp=%.4f rr=%.2f:1 (stop=%.2f%% take=%.2f%%)",
                symbol_to_inst_id(symbol),
                stop_price,
                take_price,
                rr,
                stop_pct * 100,
                take_pct * 100,
            )

    def update_stop_loss_to_break_even(self, symbol: str, side: str, entry_price: float) -> None:
        """Update the stop loss algo order to break-even (entry price) for a position.
        
        This cancels the existing SL algo order and places a new one at the entry price.
        """
        inst_id = symbol_to_inst_id(symbol)
        position_side = self._position_side_for_order(side)
        close_side = "sell" if side == "long" else "buy"
        
        # Get current position size
        pos = self.fetch_all_positions().get(symbol, {})
        amount = float(pos.get("contracts", 0))
        if amount <= 0:
            log.warning("cannot update SL for %s: no position found", symbol)
            return
        
        stop_sz = str(amount)
        
        # Place new SL at entry price (break-even)
        sl_algo_body = {
            "instId": inst_id,
            "marginMode": self._default_margin_mode(),
            "positionSide": position_side,
            "side": close_side,
            "size": stop_sz,
            "triggerPrice": f"{entry_price:.8f}".rstrip("0").rstrip("."),
            "triggerPriceType": "last",
            "orderType": "conditional",
            "brokerId": self.settings.broker_id,
        }
        
        try:
            result = self.http.place_algo_order(sl_algo_body)
            log.info("Break-even SL placed for %s @ entry %.4f -> %s", inst_id, entry_price, result)
        except Exception as e:
            log.warning("Break-even SL update failed for %s: %s", inst_id, e)

    def update_trailing_stop(self, symbol: str, side: str, trail_price: float, trail_dist_pct: float) -> None:
        """Update the stop loss to trail behind price."""
        inst_id = symbol_to_inst_id(symbol)
        position_side = self._position_side_for_order(side)
        close_side = "sell" if side == "long" else "buy"
        
        pos = self.fetch_all_positions().get(symbol, {})
        amount = float(pos.get("contracts", 0))
        if amount <= 0:
            return
        
        stop_sz = str(amount)
        
        if side == "long":
            sl_price = trail_price * (1 - trail_dist_pct)
        else:
            sl_price = trail_price * (1 + trail_dist_pct)
        
        sl_algo_body = {
            "instId": inst_id,
            "marginMode": self._default_margin_mode(),
            "positionSide": position_side,
            "side": close_side,
            "size": stop_sz,
            "triggerPrice": f"{sl_price:.8f}".rstrip("0").rstrip("."),
            "triggerPriceType": "last",
            "orderType": "conditional",
            "brokerId": self.settings.broker_id,
        }
        
        try:
            result = self.http.place_algo_order(sl_algo_body)
            log.debug("Trailing SL updated for %s @ %.4f -> %s", inst_id, sl_price, result)
        except Exception:
            pass

    def cancel_pending_tpsl(self, symbol: str) -> int:
        """Cancel all pending TP/SL algos for a symbol so leverage can be adjusted."""
        inst_id = symbol_to_inst_id(symbol)
        n = 0
        for row in self._safe_request(self.http.get_pending_tpsl, inst_id) or []:
            tid = row.get("tpslId")
            if tid:
                try:
                    self.http.cancel_tpsl(inst_id, str(tid))
                    n += 1
                except Exception:
                    pass
        if n:
            time.sleep(0.12)
        return n

    def ensure_leverage(self, symbol: str, position_side: str = "net", leverage: int | None = None) -> int:
        """Set symbol leverage to planned/mission value (exchange cap only). Returns leverage applied."""
        if api_backoff.is_paused():
            cached = self.leverage_intel.last_set(symbol)
            return cached or int(leverage if leverage else self.settings.leverage)
        exchange_cap = self.symbol_leverage_cap(symbol)
        if leverage is not None:
            desired = mission_safe_leverage(
                self.settings, exchange_cap, planned=int(leverage)
            )
        else:
            desired = mission_safe_leverage(self.settings, exchange_cap)
        side = position_side if self._hedge_mode else "net"
        pos = self.fetch_all_positions().get(symbol)
        if pos:
            side = self._position_side_for_order(str(pos.get("side") or "long"), pos)
        self._throttle()
        applied = self.leverage_intel.ensure(
            self.http,
            symbol,
            desired=desired,
            position_side=side,
            margin_mode=self._default_margin_mode(),
            cancel_tpsl_fn=self.cancel_pending_tpsl,
        )
        return applied

    def check_liquidation_proximity(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        leverage: int | None = None,
    ) -> dict[str, Any]:
        """Check how close the current price is to the estimated liquidation level.

        Returns a dict:
          - "safe": True if price is well away from liquidation
          - "distance_pct": how far from entry to liquidation (decimal)
          - "remaining_pct": how much of that gap is left (0 = at liq, 1 = at entry)
          - "liquidation_price": the estimated liquidation price
          - "exit_early": True if we should close before liquidation hits
        """
        lev = leverage if leverage else self.settings.leverage
        total_distance = self._liquidation_distance_pct(lev)  # e.g. 0.15 for 10x

        if side == "long":
            liquidation_price = entry_price * (1 - total_distance)
            travelled = entry_price - current_price
            # How much of the liquidation gap has been consumed
            if travelled <= 0:
                remaining_pct = 1.0  # price moving away from liq (profitable)
            else:
                remaining_pct = max(0.0, 1.0 - (travelled / (entry_price * total_distance)))
        else:
            liquidation_price = entry_price * (1 + total_distance)
            travelled = current_price - entry_price
            if travelled <= 0:
                remaining_pct = 1.0
            else:
                remaining_pct = max(0.0, 1.0 - (travelled / (entry_price * total_distance)))

        exit_factor = float(
            getattr(self.settings, "pre_liquidation_exit_factor", PRE_LIQUIDATION_EXIT_FACTOR)
            or PRE_LIQUIDATION_EXIT_FACTOR
        )
        exit_early = remaining_pct < exit_factor

        return {
            "safe": not exit_early,
            "distance_pct": total_distance,
            "remaining_pct": remaining_pct,
            "liquidation_price": liquidation_price,
            "exit_early": exit_early,
        }

    def fetch_recent_close_fill(
        self,
        symbol: str,
        side: str,
        *,
        opened_at: float | None = None,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """Most recent exchange fill that closed this position (fillPnl when available)."""
        inst_id = symbol_to_inst_id(symbol)
        begin_ms: int | None = None
        if opened_at and opened_at > 0:
            begin_ms = int((opened_at - 15) * 1000)
        rows = self._safe_request(
            lambda: self.http.get_fills_history(
                inst_id=inst_id,
                begin=begin_ms,
                limit=limit,
            )
        ) or []
        if not rows:
            return None

        side_l = side.lower()
        close_side = "buy" if side_l == "short" else "sell"
        pos_side = self._position_side_for_order(side_l)
        min_ts = int((opened_at - 15) * 1000) if opened_at and opened_at > 0 else 0

        def _score(row: dict[str, Any]) -> tuple[int, int, int]:
            ps = (row.get("positionSide") or "").lower()
            side_ok = (row.get("side") or "").lower() == close_side
            ps_ok = ps in ("", "net", pos_side)
            ts = int(row.get("ts") or 0)
            pnl = float(row.get("fillPnl") or 0)
            if not side_ok or not ps_ok or ts < min_ts:
                return (-1, 0, ts)
            pnl_rank = 2 if abs(pnl) > 1e-10 else 1
            return (pnl_rank, 1, ts)

        best_row: dict[str, Any] | None = None
        best_key = (-1, 0, 0)
        for row in rows:
            key = _score(row)
            if key[0] < 0:
                continue
            if key > best_key:
                best_key = key
                best_row = row

        if not best_row:
            return None
        px = float(best_row.get("fillPrice") or 0)
        if px <= 0:
            return None
        fill_pnl_ratio = None
        for key in ("fillPnlRatio", "pnlRatio", "roe", "uplRatio"):
            raw = best_row.get(key)
            if raw is not None and raw != "":
                try:
                    fill_pnl_ratio = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
        return {
            "fill_price": px,
            "fill_pnl": float(best_row.get("fillPnl") or 0),
            "fill_pnl_ratio": fill_pnl_ratio,
            "ts": int(best_row.get("ts") or 0),
            "trade_id": best_row.get("tradeId"),
        }

    def fetch_recent_tpsl_event(
        self,
        symbol: str,
        side: str,
        *,
        opened_at: float | None = None,
        limit: int = 30,
    ) -> str | None:
        """Return exchange_close reason hint from recent effective TP/SL history."""
        inst_id = symbol_to_inst_id(symbol)
        rows = self._safe_request(
            lambda: self.http.get_tpsl_history(inst_id=inst_id, limit=limit)
        ) or []
        if not rows:
            return None
        pos_side = self._position_side_for_order(side.lower())
        min_ms = int((opened_at - 15) * 1000) if opened_at and opened_at > 0 else 0
        for row in rows:
            state = (row.get("state") or "").lower()
            if state not in ("effective", "filled"):
                continue
            ps = (row.get("positionSide") or "").lower()
            if ps not in ("", "net", pos_side):
                continue
            ts = int(row.get("createTime") or row.get("updateTime") or 0)
            if ts and ts < min_ms:
                continue
            cat = (row.get("orderCategory") or row.get("triggerType") or "").lower()
            if cat == "tp":
                return "exchange_tp"
            if cat == "sl":
                return "exchange_sl"
            if cat in ("full_liquidation", "partial_liquidation", "adl"):
                return "exchange_sl"
        return None

    def close_position(self, symbol: str, position: dict[str, Any], dry_run: bool, size: float | None = None) -> None:
        """Close a position. If size is specified, close that many contracts (partial close)."""
        inst_id = symbol_to_inst_id(symbol)
        pos_side = position.get("side") or "long"
        position_side = ("long" if pos_side == "long" else "short") if self._hedge_mode else "net"
        
        if size is not None:
            log.info("partial close %s side=%s size=%s dry_run=%s", inst_id, position_side, size, dry_run)
            if dry_run:
                return
            self._throttle()
            mm = self._margin_mode_for_position(position)
            self.http.partial_close_position(
                inst_id,
                size=size,
                margin_mode=mm,
                position_side=position_side,
                broker_id=self.settings.broker_id,
            )
        else:
            mm = self._margin_mode_for_position(position)
            log.info("close %s side=%s marginMode=%s dry_run=%s", inst_id, position_side, mm, dry_run)
            if dry_run:
                return
            self._throttle()
            self.http.close_position(
                inst_id,
                margin_mode=mm,
                position_side=position_side,
                broker_id=self.settings.broker_id,
            )
        time.sleep(0.15)