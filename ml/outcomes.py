"""Track real-trade outcomes (win / loss) and their entry feature vectors
for feedback into retraining."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from margin_mode import normalize_margin_mode
from markov_regime import get_markov_engine
from symbol_quality import SymbolQualityStore

log = logging.getLogger(__name__)

_MARKETS_CT_CACHE: dict[str, float] = {}


def _contract_size_for_symbol(state_dir: Path, symbol: str) -> float:
    """Contract value (ctVal) from cached markets — default 1.0."""
    if symbol in _MARKETS_CT_CACHE:
        return _MARKETS_CT_CACHE[symbol]
    path = state_dir / "markets_cache.json"
    ct = 1.0
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            markets = raw if isinstance(raw, list) else raw.get("markets") or []
            for m in markets:
                if str(m.get("symbol") or "") == symbol:
                    ct = float(m.get("contract_size") or m.get("ctVal") or 1.0)
                    break
        except Exception:
            pass
    _MARKETS_CT_CACHE[symbol] = ct
    return ct


def sl_tp_prices(
    side: str,
    entry: float,
    *,
    stop_pct: float = 0.012,
    take_pct: float = 0.022,
    stop_price: float = 0.0,
    take_price: float = 0.0,
) -> tuple[float, float]:
    """Resolve SL/TP trigger prices from pct or absolute levels."""
    if entry <= 0:
        return stop_price, take_price
    side_l = side.lower()
    if side_l == "long":
        sl = stop_price if stop_price > 0 else entry * (1 - stop_pct)
        tp = take_price if take_price > 0 else entry * (1 + take_pct)
    else:
        sl = stop_price if stop_price > 0 else entry * (1 + stop_pct)
        tp = take_price if take_price > 0 else entry * (1 - take_pct)
    return sl, tp


def notify_trade_close(
    tracker: "TradeOutcomeTracker | None",
    symbol: str,
    side: str,
    entry: float,
    close_price: float,
    *,
    stop_pct: float = 0.012,
    take_pct: float = 0.022,
    stop_price: float = 0.0,
    take_price: float = 0.0,
    reason: str = "close",
    fill_pnl: float | None = None,
    leverage: int | None = None,
    margin_usdt: float | None = None,
    contracts: float | None = None,
    fill_pnl_ratio: float | None = None,
) -> dict[str, Any] | None:
    """Label a closed trade for ML feedback, symbol quality, and Markov."""
    if tracker is None or entry <= 0 or close_price <= 0:
        return None
    sl, tp = sl_tp_prices(
        side,
        entry,
        stop_pct=stop_pct,
        take_pct=take_pct,
        stop_price=stop_price,
        take_price=take_price,
    )
    return tracker.record_close(
        symbol,
        str(side).lower(),
        close_price,
        entry,
        sl,
        tp,
        reason=reason,
        fill_pnl=fill_pnl,
        leverage=leverage,
        margin_usdt=margin_usdt,
        contracts=contracts,
        fill_pnl_ratio=fill_pnl_ratio,
    )


def count_outcome_wins_since(state_dir: Path, since_ts: float) -> int:
    """Wins from labelled trade_outcomes.jsonl (TP hits and positive closes)."""
    path = state_dir / "trade_outcomes.jsonl"
    if not path.exists():
        return 0
    from scalp_optimizer import _parse_ts

    wins = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-4000:]:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            if _parse_ts(row.get("close_ts", row.get("ts", 0))) < since_ts:
                continue
            if row.get("outcome") == "win":
                wins += 1
            elif row.get("outcome") == "neutral" and row.get("win"):
                wins += 1
    except Exception:
        return 0
    return wins


def classify_exchange_close(
    side: str,
    entry: float,
    close_px: float,
    sl: float,
    tp: float,
    *,
    fill_pnl: float | None = None,
    tpsl_hint: str | None = None,
) -> str:
    """Infer close reason from fill PnL, TP/SL history, or proximity to triggers."""
    if tpsl_hint in ("exchange_tp", "exchange_sl"):
        return tpsl_hint
    if fill_pnl is not None:
        if fill_pnl > 1e-8:
            return "exchange_tp"
        if fill_pnl < -1e-8:
            return "exchange_sl"

    tol = max(entry * 0.0005, 1e-8)
    side_l = side.lower()
    if side_l == "long":
        if close_px >= tp - tol:
            return "exchange_tp"
        if close_px <= sl + tol:
            return "exchange_sl"
    else:
        if close_px <= tp + tol:
            return "exchange_tp"
        if close_px >= sl - tol:
            return "exchange_sl"
    return "exchange_close"


def resolve_exchange_close(
    ex: Any,
    symbol: str,
    side: str,
    entry: float,
    *,
    stop_pct: float,
    take_pct: float,
    opened_at: float | None = None,
) -> tuple[float, str, str, float | None, float | None]:
    """
    Resolve close price and reason for a vanished exchange position.
    Returns (close_px, reason, source, fill_pnl, fill_pnl_ratio).
    """
    sl, tp = sl_tp_prices(side, entry, stop_pct=stop_pct, take_pct=take_pct)
    fill_pnl: float | None = None
    fill_pnl_ratio: float | None = None
    tpsl_hint: str | None = None
    close_px = 0.0
    source = "entry"

    fetch_fill = getattr(ex, "fetch_recent_close_fill", None)
    if callable(fetch_fill):
        try:
            fill = fetch_fill(symbol, side, opened_at=opened_at)
            if fill and float(fill.get("fill_price") or 0) > 0:
                close_px = float(fill["fill_price"])
                fill_pnl = float(fill.get("fill_pnl") or 0)
                raw_ratio = fill.get("fill_pnl_ratio")
                if raw_ratio is not None:
                    fill_pnl_ratio = float(raw_ratio)
                source = "fill"
        except Exception:
            log.debug("fill lookup failed for %s", symbol, exc_info=True)

    fetch_tpsl = getattr(ex, "fetch_recent_tpsl_event", None)
    if callable(fetch_tpsl):
        try:
            tpsl_hint = fetch_tpsl(symbol, side, opened_at=opened_at)
        except Exception:
            log.debug("tpsl history lookup failed for %s", symbol, exc_info=True)

    if close_px <= 0:
        if getattr(ex, "stream", None):
            close_px = float(ex.stream.get_last_price(symbol) or 0)
            if close_px > 0:
                source = "ticker"
        if close_px <= 0:
            try:
                from markets import symbol_to_inst_id

                inst = symbol_to_inst_id(symbol)
                ex._throttle()
                tick = ex.http.get_ticker(inst)
                close_px = float(tick.get("last") or tick.get("lastPrice") or 0)
                if close_px > 0:
                    source = "ticker"
            except Exception:
                close_px = 0.0
        if close_px <= 0:
            close_px = entry
            source = "entry"

    reason = classify_exchange_close(
        side, entry, close_px, sl, tp, fill_pnl=fill_pnl, tpsl_hint=tpsl_hint
    )
    if source == "fill" and reason == "exchange_close" and fill_pnl is not None:
        if fill_pnl > 1e-8:
            reason = "exchange_tp"
        elif fill_pnl < -1e-8:
            reason = "exchange_sl"
    return close_px, reason, source, fill_pnl, fill_pnl_ratio


def label_registry_closes(
    registry: Any,
    open_symbols: set[str],
    ex: Any,
    tracker: "TradeOutcomeTracker | None",
    *,
    engine: Any = None,
) -> int:
    """
    Positions gone from the exchange (TP/SL fill) but still in registry — label outcomes.
    Call before registry.sync_with_exchange().
    """
    if tracker is None:
        registry.sync_with_exchange(open_symbols)
        return 0

    labeled = 0
    for symbol in registry.stale_symbols(open_symbols):
        meta = registry.pop_meta(symbol) or {}
        side = str(meta.get("side") or "long")
        entry = float(meta.get("entry_price") or 0)
        if entry <= 0:
            continue
        stop_pct = float(meta.get("stop_pct") or 0.012)
        take_pct = float(meta.get("take_pct") or 0.022)
        opened_at = float(meta.get("opened_at") or 0) or None
        close_px, reason, src, fill_pnl, fill_pnl_ratio = resolve_exchange_close(
            ex,
            symbol,
            side,
            entry,
            stop_pct=stop_pct,
            take_pct=take_pct,
            opened_at=opened_at,
        )
        lev = int(meta.get("leverage") or 0) or None
        margin = float(meta.get("margin_usdt") or 0)
        contracts = float(meta.get("contracts") or 0) or None
        if src == "fill":
            log.info(
                "close fill %s %s px=%.6f reason=%s (fill history)",
                symbol,
                side,
                close_px,
                reason,
            )
        elif src == "ticker" and reason in ("exchange_tp", "exchange_sl"):
            log.info(
                "close inferred %s %s px=%.6f reason=%s (ticker+sl/tp)",
                symbol,
                side,
                close_px,
                reason,
            )
        rec = notify_trade_close(
            tracker,
            symbol,
            side,
            entry,
            close_px,
            stop_pct=stop_pct,
            take_pct=take_pct,
            reason=reason,
            fill_pnl=fill_pnl,
            leverage=lev,
            margin_usdt=margin if margin > 0 else None,
            contracts=contracts,
            fill_pnl_ratio=fill_pnl_ratio,
        )
        if rec:
            labeled += 1
            if engine is not None:
                from exchange_client import BlofinExchange

                market = getattr(ex, "market_for", lambda _s: None)(symbol)
                ct_size = float(getattr(market, "contract_size", None) or 1.0)
                pnl_usd = BlofinExchange.estimate_realized_pnl_usd(
                    side=side,
                    entry=entry,
                    exit_px=close_px,
                    fill_pnl=fill_pnl,
                    margin_usdt=margin or None,
                    leverage=lev,
                    contracts=contracts,
                    contract_size=ct_size,
                    roe_pct=rec.get("roe_pct"),
                )
                engine.record_closed_trade(
                    symbol,
                    pnl_usd,
                    side=side,
                    event=reason,
                    roe_pct=rec.get("roe_pct"),
                    entry=entry,
                    exit_px=close_px,
                    leverage=lev,
                    margin_usdt=margin if margin > 0 else None,
                )
    registry.sync_with_exchange(open_symbols)
    if labeled:
        log.info("labeled %d exchange-closed position(s) for ML feedback", labeled)
    return labeled


class TradeOutcomeTracker:
    """Records entry-time ML features when a position opens and, upon close,
    labels the outcome as win (hit TP) or loss (hit SL) so the model can
    learn from its own live decisions."""

    def __init__(self, state_dir: Path, max_samples: int = 500) -> None:
        self.path = state_dir / "trade_outcomes.jsonl"
        self.max_samples = max_samples
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir
        self._quality = SymbolQualityStore(state_dir)

    # ------------------------------------------------------------------
    # Recording entry features when a position is opened
    # ------------------------------------------------------------------
    def record_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        take_price: float,
        feature_vector: list[float],
        signal_score: float,
        timestamp_ms: int | None = None,
        markov_state: str = "",
        markov_stress_p: float = 0.0,
        *,
        run_score: float | None = None,
        path_efficiency: float | None = None,
        chop_index: float | None = None,
        run_label: str = "",
        pick_score: float | None = None,
        curve_phase: str = "",
        margin_mode: str = "isolated",
    ) -> None:
        """Store the feature vector observed at entry time, plus metadata,
        so we can later label outcome (win / loss)."""
        record = {
            "event": "entry",
            "symbol": symbol,
            "side": side,
            "entry_price": round(entry_price, 8),
            "stop_price": round(stop_price, 8),
            "take_price": round(take_price, 8),
            "feature_vector": [round(v, 8) for v in feature_vector],
            "signal_score": signal_score,
            "ts": int(timestamp_ms or time.time() * 1000),
            "markov_state": markov_state,
            "markov_stress_p": float(markov_stress_p),
            "run_score": round(float(run_score), 4) if run_score is not None else None,
            "path_efficiency": round(float(path_efficiency), 4) if path_efficiency is not None else None,
            "chop_index": round(float(chop_index), 4) if chop_index is not None else None,
            "run_label": run_label or "",
            "pick_score": round(float(pick_score), 4) if pick_score is not None else None,
            "curve_phase": curve_phase or "",
            "margin_mode": normalize_margin_mode(margin_mode),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self._quality.note_open(symbol)
        self._quality.save()
        log.debug("recorded entry for %s %s", symbol, side)

    # ------------------------------------------------------------------
    # Label outcome when a position closes
    # ------------------------------------------------------------------
    def record_close(
        self,
        symbol: str,
        side: str,
        close_price: float,
        entry_price: float,
        stop_price: float,
        take_price: float,
        reason: str = "unknown",
        fill_pnl: float | None = None,
        leverage: int | None = None,
        margin_usdt: float | None = None,
        contracts: float | None = None,
        fill_pnl_ratio: float | None = None,
    ) -> dict[str, Any] | None:
        """Find the most recent unmatched entry for (symbol, side) and label
        outcome.  Returns the labelled record or None."""
        entry = self._find_entry(symbol, side)
        if entry is None:
            log.warning("no matching entry for close %s %s", symbol, side)
            return None

        reason_l = str(reason).lower()
        if "tp" in reason_l or reason_l in ("harvest", "tp_backup_close"):
            outcome = "win"
        elif "sl" in reason_l or reason_l in ("sl_backup_close", "upgrade_out"):
            outcome = "loss" if "upgrade" not in reason_l else "neutral"
        else:
            ep = float(entry.get("entry_price") or entry_price)
            if side == "long":
                if close_price >= take_price:
                    outcome = "win"
                elif close_price <= stop_price:
                    outcome = "loss"
                else:
                    gross = (close_price - ep) / max(ep, 1e-12)
                    outcome = "win" if gross > 0.0005 else ("loss" if gross < -0.0005 else "neutral")
            else:
                if close_price <= take_price:
                    outcome = "win"
                elif close_price >= stop_price:
                    outcome = "loss"
                else:
                    gross = (ep - close_price) / max(ep, 1e-12)
                    outcome = "win" if gross > 0.0005 else ("loss" if gross < -0.0005 else "neutral")

        label = 0 if side == "long" else 1  # same as training: 0=long, 1=short
        win_flag = 1 if outcome == "win" else 0
        ep = float(entry.get("entry_price") or entry_price)
        lev = int(leverage or 0) or None
        margin = float(margin_usdt or entry.get("margin_usdt") or 0)
        contracts_v = float(contracts or entry.get("contracts") or 0) or None
        from roe_learning import journal_open_before, resolve_close_pnl_roe

        if margin <= 0 or not contracts_v:
            j_margin, j_contracts, j_lev = journal_open_before(
                self.state_dir, symbol, time.time()
            )
            if margin <= 0 and j_margin > 0:
                margin = j_margin
            if not contracts_v and j_contracts > 0:
                contracts_v = j_contracts
            if not lev and j_lev > 0:
                lev = j_lev

        ct_size = _contract_size_for_symbol(self.state_dir, symbol)
        pnl_usd, roe_pct = resolve_close_pnl_roe(
            side=side,
            entry=ep,
            exit_px=float(close_price),
            fill_pnl=fill_pnl,
            margin_usdt=margin if margin > 0 else None,
            leverage=lev,
            contracts=contracts_v,
            contract_size=ct_size,
            fill_pnl_ratio=fill_pnl_ratio,
        )
        if roe_pct is not None and roe_pct > 0 and outcome == "loss":
            outcome = "win"
            win_flag = 1
        elif roe_pct is not None and roe_pct < 0 and outcome == "win":
            outcome = "loss"
            win_flag = 0

        if outcome == "win":
            r_mult = 3.0 if (roe_pct or 0) >= 40 else max(0.5, (roe_pct or 10.0) / 35.0)
        elif outcome == "loss":
            r_mult = -1.0
        else:
            r_mult = 0.0

        record: dict[str, Any] = {
            "event": "outcome",
            "symbol": symbol,
            "side": side,
            "outcome": outcome,
            "label": label,
            "win": win_flag,
            "r_multiple": round(float(r_mult), 3),
            "entry_price": entry.get("entry_price"),
            "close_price": round(close_price, 8),
            "stop_price": stop_price,
            "take_price": take_price,
            "entry_ts": entry.get("ts"),
            "close_ts": int(time.time() * 1000),
            "feature_vector": entry.get("feature_vector"),
            "signal_score": entry.get("signal_score", 0),
            "reason": reason,
            "roe_pct": roe_pct,
            "fill_pnl": round(pnl_usd, 6) if pnl_usd else None,
            "leverage": lev,
            "margin_usdt": round(margin, 6) if margin > 0 else None,
            "contracts": contracts_v,
            "run_score": entry.get("run_score"),
            "path_efficiency": entry.get("path_efficiency"),
            "chop_index": entry.get("chop_index"),
            "run_label": entry.get("run_label"),
            "pick_score": entry.get("pick_score"),
            "curve_phase": entry.get("curve_phase"),
            "margin_mode": normalize_margin_mode(entry.get("margin_mode") or "isolated"),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        try:
            from config import load_settings
            from trade_lessons import on_trade_close

            on_trade_close(load_settings(), record)
        except Exception:
            log.debug("trade lesson hook failed", exc_info=True)
        self._quality.note_outcome(symbol, win=bool(win_flag), roe_pct=roe_pct)
        if entry.get("run_label") == "runner" and win_flag:
            self._quality.note_run_quality(
                symbol, run_score=float(entry.get("run_score") or 0.6), label="runner", is_runner=True
            )
        elif entry.get("run_label") == "choppy" and not win_flag:
            self._quality.note_run_quality(
                symbol, run_score=float(entry.get("run_score") or 0.2), label="choppy", is_choppy=True
            )
        self._quality.save()
        try:
            get_markov_engine(self.state_dir).observe_outcome(
                state=str(entry.get("markov_state") or ""),
                win=bool(win_flag),
                stress_p=float(entry.get("markov_stress_p") or 0.0),
            )
        except Exception:
            log.debug("markov outcome update failed", exc_info=True)

        log.info(
            "outcome %s %s %s entry=%.4f close=%.4f roe=%s pnl=$%.4f sl=%.4f tp=%.4f",
            symbol,
            side,
            outcome,
            entry["entry_price"],
            close_price,
            f"{roe_pct:+.1f}%" if roe_pct is not None else "—",
            pnl_usd,
            stop_price,
            take_price,
        )
        return record

    # ------------------------------------------------------------------
    # Load labelled outcomes for merging into training
    # ------------------------------------------------------------------
    def load_labelled_samples(
        self,
        max_samples: int | None = None,
        *,
        margin_mode: str | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (X_feedback, y_feedback) from recorded outcomes for
        inclusion in training."""
        want_mode = normalize_margin_mode(margin_mode) if margin_mode else None
        limit = max_samples or self.max_samples
        rows: list[dict[str, Any]] = []
        if not self.path.exists():
            return np.empty((0, 0)), np.empty((0,))

        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "outcome" or row.get("outcome") == "neutral":
                    continue
                if want_mode:
                    row_mode = normalize_margin_mode(row.get("margin_mode") or "isolated")
                    if row_mode != want_mode:
                        continue
                rows.append(row)

        # Keep most recent samples up to limit
        rows = rows[-limit:]

        if not rows:
            return np.empty((0, 0)), np.empty((0,))

        X_list: list[np.ndarray] = []
        y_list: list[int] = []

        from ml.features import FEATURE_NAMES

        target_n = len(FEATURE_NAMES)
        for row in rows:
            fv = row.get("feature_vector")
            if not fv or not isinstance(fv, list):
                continue
            if len(fv) != target_n:
                if len(fv) < target_n:
                    fv = list(fv) + [0.0] * (target_n - len(fv))
                else:
                    fv = fv[:target_n]
            X_list.append(np.array(fv, dtype=np.float64))
            # label: 0 = long, 1 = short  (same as training)
            y_list.append(int(row["label"]))

        if not X_list:
            return np.empty((0, 0)), np.empty((0,))

        X = np.vstack(X_list)
        y = np.array(y_list, dtype=np.int64)
        msg = "loaded %d real-feedback samples for retraining" % len(y)
        if want_mode:
            msg += f" (margin_mode={want_mode})"
        log.info(msg)
        return X, y

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _find_entry(self, symbol: str, side: str) -> dict[str, Any] | None:
        """Scan outcomes.jsonl backwards to find the most recent 'entry'
        record for (symbol, side) that has no matching 'outcome' record
        after it.  We detect matching by scanning forward from an entry
        and checking if any 'outcome' with the same symbol/side exists."""
        if not self.path.exists():
            return None

        with self.path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()

        # Find all entry and outcome indices for this symbol/side
        entry_indices: list[int] = []
        outcome_indices: set[int] = set()

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = row.get("event")
            sym = row.get("symbol")
            sd = row.get("side")
            if sym != symbol or sd != side:
                continue
            if evt == "entry":
                entry_indices.append(i)
            elif evt == "outcome":
                outcome_indices.add(i)

        # Find the latest entry that has no outcome after it
        for ei in reversed(entry_indices):
            # Check no outcome index > ei
            has_outcome = any(oi > ei for oi in outcome_indices)
            if not has_outcome:
                return json.loads(lines[ei])
        return None