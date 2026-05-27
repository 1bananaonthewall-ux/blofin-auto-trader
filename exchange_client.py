from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from blofin_http import BlofinHttp
from config import Settings
from liquidation_guard import (
    effective_leverage,
    margin_rate,
    sl_is_safe,
    sl_tp_from_exchange_liq,
    trigger_prices,
)
from leverage_intel import LeverageIntel, leverage_needs_reentry
from markets import Market, symbol_to_inst_id
from scalp_profile import profile_for

if TYPE_CHECKING:
    from market_stream import BlofinMarketStream

log = logging.getLogger(__name__)

# How close (as multiplier of liquidation distance) we allow price
# to get before proactively exiting.  E.g. 0.5 means if price is within
# 50% of the gap between entry and liquidation, we exit early.
PRE_LIQUIDATION_EXIT_FACTOR = 0.65


def _tpsl_profile_kwargs(settings: Settings) -> dict[str, float | bool]:
    prof = profile_for(settings)
    if prof and prof.three_r_mode:
        return {"min_rr": prof.min_rr, "enforce_tp_from_sl": True}
    return {"min_rr": prof.min_rr if prof else 1.25, "enforce_tp_from_sl": False}


class BlofinExchange:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = BlofinHttp(
            settings.api_key,
            settings.secret,
            settings.passphrase,
            demo=settings.mode == "demo",
        )
        self._hedge_mode = True
        self.markets: dict[str, Market] = {}
        self._scan_offset = 0
        self._last_api_call = 0.0
        self._min_api_gap = 0.15  # 150ms between API calls
        # Cache for VWAP calculation
        self._vwap_cache: dict[str, dict] = {}
        self.stream: BlofinMarketStream | None = None
        self.last_open_error: str = ""
        self.last_repaired_tpsl: tuple[float, float] | None = None
        self.leverage_intel = LeverageIntel(settings.state_dir)

    def attach_stream(self, stream: BlofinMarketStream) -> None:
        self.stream = stream

    def _throttle(self):
        """Ensure minimum gap between API calls to avoid rate limiting."""
        elapsed = time.time() - self._last_api_call
        if elapsed < self._min_api_gap:
            time.sleep(self._min_api_gap - elapsed)
        self._last_api_call = time.time()

    def _safe_request(self, method, *args, retries=3, **kwargs):
        """Make an API request with retry on failure."""
        last_error = None
        for attempt in range(retries):
            self._throttle()
            try:
                result = method(*args, **kwargs)
                if result is not None:
                    return result
            except Exception as e:
                last_error = e
                log.debug("API call failed (attempt %d/%d): %s", attempt + 1, retries, e)
                time.sleep(1.0 * (attempt + 1))  # backoff
        log.warning("API call failed after %d retries: %s", retries, last_error)
        return None

    def load(self) -> None:
        mode = self._safe_request(lambda: self.http.request("GET", "/api/v1/account/position-mode")) or {}
        self._hedge_mode = (mode.get("positionMode") or "") == "long_short_mode"
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
                return self.fetch_equity_usdt()
            details = bal.get('details', []) if isinstance(bal, dict) else bal
            if isinstance(bal, dict):
                for row in details:
                    if row.get('currency') == 'USDT':
                        return float(row.get('availableEquity', 0.0))
            return self.fetch_equity_usdt()
        except Exception:
            log.warning("failed to fetch free equity, using total equity")
            return self.fetch_equity_usdt()

    def fetch_equity_usdt(self) -> float:
        data = self._safe_request(self.http.get_balance)
        if data is None:
            return 0.0
        if isinstance(data, dict):
            total = data.get("totalEquity") or data.get("equity")
            if total is not None:
                return float(total)
        if isinstance(data, list):
            for row in data:
                if row.get("currency") in ("USDT", None) or row.get("ccy") == "USDT":
                    return float(row.get("equity") or row.get("available") or 0)
        return 0.0

    def fetch_all_positions(self) -> dict[str, dict[str, Any]]:
        rows = self._safe_request(self.http.get_positions)
        if rows is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            inst_id = row.get("instId") or ""
            size = float(row.get("positions") or row.get("pos") or row.get("size") or 0)
            if abs(size) <= 0:
                continue
            symbol = f"{inst_id.replace('-USDT', '')}/USDT:USDT"
            side = (row.get("positionSide") or row.get("side") or "").lower()
            if side == "net":
                side = "long" if size > 0 else "short"
            entry = float(row.get("avgPx") or row.get("avgPrice") or row.get("averagePrice") or 0)
            mark = float(row.get("markPrice") or entry or 0)
            margin = float(row.get("margin") or row.get("initialMargin") or 0)
            liq = float(row.get("liquidationPrice") or 0)
            lev = int(float(row.get("leverage") or self.settings.leverage or 10))
            mkt = self.market_for(symbol)
            cs = mkt.contract_size if mkt else 1.0
            notional = abs(size) * cs * mark if mark > 0 else 0.0
            out[symbol] = {
                "contracts": abs(size),
                "side": side,
                "entry_price": entry,
                "mark_price": mark,
                "margin_usdt": margin,
                "liquidation_price": liq,
                "leverage": lev,
                "notional_usdt": notional,
                "margin_rate": margin_rate(notional, margin, lev) if margin > 0 else 0.0,
                "effective_leverage": effective_leverage(notional, margin, lev) if margin > 0 else lev,
                "info": row,
            }
        return out

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

    def _position_side_for_order(self, side: str) -> str:
        if not self._hedge_mode:
            return "net"
        return "long" if side == "long" else "short"

    def _format_price(self, price: float) -> str:
        return f"{price:.8f}".rstrip("0").rstrip(".")

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
        amount = max(min_size, round(contracts / min_size) * min_size)

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

        body = {
            "instId": inst_id,
            "marginMode": "isolated",
            "positionSide": position_side,
            "side": order_side,
            "orderType": "market",
            "size": str(amount),
            "brokerId": self.settings.broker_id,
        }

        log.info(
            "OPEN %s %s size=%s @~%.4f lev=%dx (TPSL after fill from exchange liq) dry=%s",
            order_side,
            inst_id,
            amount,
            price,
            lev,
            dry_run,
        )
        if dry_run:
            return None

        self.ensure_leverage(symbol, position_side, leverage)
        time.sleep(0.12)
        try:
            result = self.http.place_order(body)
            time.sleep(0.35)
            ok, rep_stop, rep_take = self.repair_position_tpsl(
                symbol,
                side,
                amount,
                take_pct=take_pct,
                configured_leverage=lev,
                dry_run=dry_run,
            )
            if ok and rep_stop > 0 and rep_take > 0:
                self.last_repaired_tpsl = (rep_stop, rep_take)
            return result
        except Exception as e:
            self.last_open_error = str(e)
            log.error("open failed %s: %s", symbol, e)
            return None

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
    ) -> tuple[bool, float, float]:
        """Attach SL/TP using exchange liquidationPrice — fixes under-margined positions."""
        inst_id = symbol_to_inst_id(symbol)
        pos = self.fetch_all_positions().get(symbol)
        if not pos:
            log.warning("repair TPSL: no position %s", symbol)
            return False, 0.0, 0.0

        entry = float(pos.get("entry_price") or 0)
        liq = float(pos.get("liquidation_price") or 0)
        margin = float(pos.get("margin_usdt") or 0)
        eff_lev = int(pos.get("effective_leverage") or configured_leverage)
        mrate = float(pos.get("margin_rate") or 0)

        buf = getattr(self.settings, "sl_liq_buffer", 0.38)
        rr_kw = _tpsl_profile_kwargs(self.settings)
        sl_trig, tp_trig, stop_pct, take_pct = sl_tp_from_exchange_liq(
            side, entry, liq, take_pct, buffer=buf, **rr_kw
        )
        rr = take_pct / max(stop_pct, 1e-9)

        min_rate = getattr(self.settings, "min_margin_rate", 0.92)
        if mrate > 0 and mrate < min_rate * 0.75:
            log.warning(
                "low margin rate %s: %.0f%% (target %.0f%%) — position under-margined on exchange",
                symbol,
                mrate * 100,
                min_rate * 100,
            )

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

        pending = self._safe_request(self.http.get_pending_tpsl, inst_id) or []
        if pending:
            cur_sl = max(float(p.get("slTriggerPrice") or 0) for p in pending)
            cur_tp = max(float(p.get("tpTriggerPrice") or 0) for p in pending)
            if (
                cur_sl > 0
                and cur_tp > 0
                and sl_is_safe(side, entry, cur_sl, liquidation_price=liq, leverage=eff_lev)
            ):
                if entry > 0:
                    if side == "long":
                        stop_pct = (entry - cur_sl) / entry
                        take_pct = (cur_tp - entry) / entry
                    else:
                        stop_pct = (cur_sl - entry) / entry
                        take_pct = (entry - cur_tp) / entry
                return True, stop_pct, take_pct

        if cancel_existing and not dry_run:
            for row in self._safe_request(self.http.get_pending_tpsl, inst_id) or []:
                tid = row.get("tpslId")
                if tid:
                    try:
                        self.http.cancel_tpsl(inst_id, str(tid))
                    except Exception:
                        pass
            time.sleep(0.15)

        self.ensure_position_tpsl(symbol, side, contracts, sl_trig, tp_trig, dry_run)
        log.info(
            "TPSL repaired %s %s entry=%.6f sl=%.6f (%.2f%%) liq=%.6f tp=%.6f (%.2f%%) rr=%.2f:1 margin=$%.3f rate=%.0f%% eff_lev=%dx",
            symbol,
            side,
            entry,
            sl_trig,
            stop_pct * 100,
            liq,
            tp_trig,
            take_pct * 100,
            rr,
            margin,
            mrate * 100,
            eff_lev,
        )
        return True, stop_pct, take_pct

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
        inst_id = symbol_to_inst_id(symbol)
        pending = self._safe_request(self.http.get_pending_tpsl, inst_id) or []
        pos = self.fetch_all_positions().get(symbol)
        entry = float(pos.get("entry_price") or 0) if pos else 0.0
        liq = float(pos.get("liquidation_price") or 0) if pos else 0.0
        eff_lev = int(pos.get("effective_leverage") or self.settings.leverage) if pos else self.settings.leverage
        buf = getattr(self.settings, "sl_liq_buffer", 0.38)

        take_pct_hint = (
            abs(take_price - entry) / entry
            if entry > 0 and take_price > 0
            else (abs(entry - stop_price) / entry if entry > 0 else 0.02)
        )
        stop_pct = take_pct = rr = 0.0
        if entry > 0:
            rr_kw = _tpsl_profile_kwargs(self.settings)
            stop_price, take_price, stop_pct, take_pct = sl_tp_from_exchange_liq(
                side, entry, liq, take_pct_hint, buffer=buf, **rr_kw
            )
            rr = take_pct / max(stop_pct, 1e-9)

        sl_ok = entry > 0 and sl_is_safe(
            side, entry, stop_price, liquidation_price=liq, leverage=eff_lev
        )
        pending_sl = [p for p in pending if float(p.get("slTriggerPrice") or 0) > 0]
        pending_tp = [p for p in pending if float(p.get("tpTriggerPrice") or 0) > 0]
        if pending_sl and pending_tp and sl_ok:
            return

        if not dry_run and pending:
            for row in pending:
                tid = row.get("tpslId")
                if tid:
                    try:
                        self.http.cancel_tpsl(inst_id, str(tid))
                    except Exception:
                        pass
            time.sleep(0.12)

        position_side = self._position_side_for_order(side)
        close_side = "sell" if side == "long" else "buy"
        body = {
            "instId": inst_id,
            "marginMode": "isolated",
            "positionSide": position_side,
            "side": close_side,
            "size": str(contracts),
            "brokerId": self.settings.broker_id,
            "reduceOnly": "true",
        }
        body["slTriggerPrice"] = self._format_price(stop_price)
        body["slOrderPrice"] = "-1"
        body["tpTriggerPrice"] = self._format_price(take_price)
        body["tpOrderPrice"] = "-1"

        if dry_run:
            return
        try:
            self.http.place_order_tpsl(body)
            log.info(
                "TPSL ensured %s sl=%.4f tp=%.4f rr=%.2f:1 (stop=%.2f%% take=%.2f%%)",
                inst_id,
                stop_price,
                take_price,
                rr if entry > 0 else 0.0,
                stop_pct * 100 if entry > 0 else 0.0,
                take_pct * 100 if entry > 0 else 0.0,
            )
        except Exception as e:
            log.warning("TPSL ensure failed %s: %s", inst_id, e)

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
            "marginMode": "isolated",
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
            "marginMode": "isolated",
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
        """Set symbol leverage (capped by exchange max). Returns leverage applied."""
        desired = int(leverage if leverage else self.settings.leverage)
        if self.settings.scalp_3r_mode:
            desired = self.symbol_leverage_cap(symbol)
        side = position_side if self._hedge_mode else "net"
        self._throttle()
        applied = self.leverage_intel.ensure(
            self.http,
            symbol,
            desired=desired,
            position_side=side,
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

        exit_early = remaining_pct < PRE_LIQUIDATION_EXIT_FACTOR

        return {
            "safe": not exit_early,
            "distance_pct": total_distance,
            "remaining_pct": remaining_pct,
            "liquidation_price": liquidation_price,
            "exit_early": exit_early,
        }

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
            self.http.partial_close_position(
                inst_id,
                size=size,
                position_side=position_side,
                broker_id=self.settings.broker_id,
            )
        else:
            log.info("close %s side=%s dry_run=%s", inst_id, position_side, dry_run)
            if dry_run:
                return
            self._throttle()
            self.http.close_position(
                inst_id,
                position_side=position_side,
                broker_id=self.settings.broker_id,
            )
        time.sleep(0.15)