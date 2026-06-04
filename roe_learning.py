"""Learn from recent trade ROE (return on margin) for optimizer + symbol gates."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from exchange_client import BlofinExchange

log = logging.getLogger(__name__)

_STATE_NAME = "roe_learning.json"
_RECENT_MAX = 120


def default_close_leverage() -> int:
    try:
        from config import load_settings

        s = load_settings()
        if getattr(s, "scalp_mode", False):
            return int(getattr(s, "scalp_leverage", 0) or getattr(s, "leverage", 0) or 50)
        return int(getattr(s, "leverage", 0) or 50)
    except Exception:
        return 50


def _parse_exchange_roe_ratio(raw: Any) -> float | None:
    """Normalize exchange fill/position ratio fields to ROE percent."""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if abs(v) <= 1.5:
        return round(v * 100.0, 2)
    return round(v, 2)


def compute_close_roe_pct(
    *,
    side: str,
    entry: float,
    exit_px: float,
    pnl_usd: float,
    leverage: int | None = None,
    margin_usdt: float | None = None,
    contracts: float | None = None,
    contract_size: float = 1.0,
    fill_pnl_ratio: float | None = None,
) -> float | None:
    """
    ROE on margin (Blofin UI): realized PnL / posted margin.
    Falls back to price move × effective leverage (notional/margin), not configured lev.
    """
    ratio_roe = _parse_exchange_roe_ratio(fill_pnl_ratio)
    if ratio_roe is not None:
        return ratio_roe

    margin = float(margin_usdt or 0)
    pnl = float(pnl_usd or 0)
    if margin > 0:
        return round((pnl / margin) * 100.0, 2)

    # Without posted margin, do not invent ROE from price×leverage (shows -200% on 4% moves).
    return None


def journal_open_before(
    state_dir: Path,
    symbol: str,
    close_ts: float,
) -> tuple[float, float, int]:
    """Last open margin/contracts/leverage from trades.jsonl before close."""
    path = state_dir / "trades.jsonl"
    if not path.is_file():
        return 0.0, 0.0, 0
    best_ts = 0.0
    margin = 0.0
    contracts = 0.0
    leverage = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-600:]
    except Exception:
        return 0.0, 0.0, 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(raw.get("event") or "").lower() != "open":
            continue
        if str(raw.get("symbol") or "") != symbol:
            continue
        raw_ts = raw.get("ts")
        if isinstance(raw_ts, (int, float)):
            ts = float(raw_ts)
            if ts > 1e12:
                ts /= 1000.0
        else:
            try:
                from datetime import datetime, timezone

                ts = datetime.fromisoformat(
                    str(raw_ts or "").replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                try:
                    ts = float(raw_ts or 0)
                    if ts > 1e12:
                        ts /= 1000.0
                except (TypeError, ValueError):
                    ts = 0.0
        if close_ts > 0 and ts > close_ts:
            continue
        if ts >= best_ts:
            best_ts = ts
            margin = float(raw.get("margin") or 0)
            contracts = float(raw.get("contracts") or 0)
            leverage = int(raw.get("leverage") or 0)
    return margin, contracts, leverage


def resolve_close_pnl_roe(
    *,
    side: str,
    entry: float,
    exit_px: float,
    fill_pnl: float | None = None,
    prof_pnl: float | None = None,
    margin_usdt: float | None = None,
    leverage: int | None = None,
    contracts: float | None = None,
    contract_size: float = 1.0,
    fill_pnl_ratio: float | None = None,
) -> tuple[float, float | None]:
    """
    Authoritative closed-trade PnL + ROE for dashboard / profitability.
    Prefers exchange fill, then profitability ledger, then sized estimate.
    """
    pnl: float | None = None
    if fill_pnl is not None and abs(float(fill_pnl)) > 1e-9:
        pnl = float(fill_pnl)
    elif prof_pnl is not None:
        pnl = float(prof_pnl)
    else:
        pnl = BlofinExchange.estimate_realized_pnl_usd(
            side=side,
            entry=entry,
            exit_px=exit_px,
            margin_usdt=margin_usdt,
            leverage=leverage,
            contracts=contracts,
            contract_size=contract_size,
        )
    roe = compute_close_roe_pct(
        side=side,
        entry=entry,
        exit_px=exit_px,
        pnl_usd=pnl,
        leverage=leverage,
        margin_usdt=margin_usdt,
        contracts=contracts,
        contract_size=contract_size,
        fill_pnl_ratio=fill_pnl_ratio,
    )
    return round(float(pnl), 6), roe


class RoeLearningStore:
    """Rolling ROE memory for scalp optimizer, engine performance, symbol quality."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / _STATE_NAME
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._data = {"global": {"recent": []}, "symbols": {}}
            self._backfill_from_outcomes()
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {"global": {"recent": []}, "symbols": {}}
        self._data.setdefault("global", {}).setdefault("recent", [])
        self._data.setdefault("symbols", {})
        if not self._data["global"]["recent"]:
            self._backfill_from_outcomes()

    def _backfill_from_outcomes(self, limit: int = 60) -> None:
        """Seed store from trade_outcomes.jsonl on first run."""
        path = self.path.parent / "trade_outcomes.jsonl"
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
        except Exception:
            return
        rows: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(raw.get("event") or "").lower() != "outcome":
                continue
            sym = str(raw.get("symbol") or "")
            ep = float(raw.get("entry_price") or 0)
            xp = float(raw.get("close_price") or 0)
            pnl = float(raw.get("fill_pnl") or 0)
            lev = int(raw.get("leverage") or 0) or None
            side = str(raw.get("side") or "long")
            roe = raw.get("roe_pct")
            if roe is None:
                roe = compute_close_roe_pct(
                    side=side,
                    entry=ep,
                    exit_px=xp,
                    pnl_usd=pnl,
                    leverage=lev,
                    margin_usdt=raw.get("margin_usdt"),
                    contracts=raw.get("contracts"),
                )
            if roe is None:
                continue
            ts = float(raw.get("close_ts") or raw.get("ts") or 0)
            if ts > 1e12:
                ts /= 1000.0
            rows.append(
                {
                    "symbol": sym,
                    "side": side,
                    "roe_pct": round(float(roe), 2),
                    "pnl_usd": round(pnl, 6),
                    "event": str(raw.get("reason") or "close"),
                    "ts": ts,
                }
            )
        if not rows:
            return
        g = self._data.setdefault("global", {})
        g["recent"] = rows[-limit:]
        for row in g["recent"]:
            sym = row["symbol"]
            sym_rows = self._data.setdefault("symbols", {}).setdefault(sym, {})
            roe = float(row["roe_pct"])
            ema = float(sym_rows.get("roe_ema") or roe)
            sym_rows["roe_ema"] = round(0.82 * ema + 0.18 * roe, 2)
            sym_rows["last_roe"] = roe
        self._recompute_global()
        self._save()
        log.info("roe learning backfilled %d closes from trade_outcomes", len(g["recent"]))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = time.time()
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def record_close(
        self,
        symbol: str,
        *,
        side: str,
        roe_pct: float | None,
        pnl_usd: float,
        event: str = "close",
        entry: float | None = None,
        exit_px: float | None = None,
        leverage: int | None = None,
        margin_usdt: float | None = None,
        contracts: float | None = None,
    ) -> dict[str, Any] | None:
        if roe_pct is None:
            if entry and exit_px:
                roe_pct = compute_close_roe_pct(
                    side=side,
                    entry=float(entry),
                    exit_px=float(exit_px),
                    pnl_usd=float(pnl_usd),
                    leverage=leverage,
                    margin_usdt=margin_usdt,
                    contracts=contracts,
                )
        if roe_pct is None:
            return None

        roe = round(float(roe_pct), 2)
        ts = time.time()
        row = {
            "symbol": symbol,
            "side": str(side).lower(),
            "roe_pct": roe,
            "pnl_usd": round(float(pnl_usd), 6),
            "event": event,
            "ts": ts,
        }
        g = self._data.setdefault("global", {})
        recent: list[dict] = list(g.get("recent") or [])
        recent.append(row)
        g["recent"] = recent[-_RECENT_MAX:]
        g["last_roe"] = roe
        g["last_symbol"] = symbol

        sym_rows = self._data.setdefault("symbols", {}).setdefault(symbol, {})
        ema = float(sym_rows.get("roe_ema") or roe)
        sym_rows["roe_ema"] = round(0.82 * ema + 0.18 * roe, 2)
        sym_rows["closes"] = int(sym_rows.get("closes") or 0) + 1
        sym_rows["last_roe"] = roe
        sym_rows["last_ts"] = ts
        if roe > 0:
            sym_rows["wins"] = int(sym_rows.get("wins") or 0) + 1
        elif roe < 0:
            sym_rows["losses"] = int(sym_rows.get("losses") or 0) + 1

        self._recompute_global()
        self._save()
        log.info("roe learn %s %s ROE %+.1f%% ($%+.4f)", symbol, side, roe, pnl_usd)
        return row

    def _recompute_global(self) -> None:
        g = self._data.setdefault("global", {})
        recent = list(g.get("recent") or [])
        if not recent:
            return
        wins = sum(1 for r in recent if float(r.get("roe_pct") or 0) > 0)
        g["win_rate_all"] = round(wins / len(recent), 4)
        roes = [float(r["roe_pct"]) for r in recent if r.get("roe_pct") is not None]
        if roes:
            g["avg_roe_all"] = round(sum(roes) / len(roes), 2)
        streak = 0
        for r in reversed(recent):
            if float(r.get("roe_pct") or 0) < 0:
                streak += 1
            else:
                break
        g["consecutive_neg_roe"] = streak

    def recent_performance(
        self,
        window_sec: float = 3600.0,
        *,
        limit: int = 40,
    ) -> tuple[float, float, int, float]:
        """
        Returns (win_rate_by_roe, profit_factor_roe, consecutive_neg_roe, avg_roe).
        Win = ROE > 0; PF = sum(pos ROE) / |sum(neg ROE)|.
        """
        cutoff = time.time() - window_sec
        recent = [
            r
            for r in (self._data.get("global", {}).get("recent") or [])
            if float(r.get("ts") or 0) >= cutoff
        ][-limit:]
        if not recent:
            return self._bootstrap_from_profitability(window_sec, limit)

        wins = sum(1 for r in recent if float(r.get("roe_pct") or 0) > 0)
        wr = wins / len(recent)
        pos = sum(float(r["roe_pct"]) for r in recent if float(r.get("roe_pct") or 0) > 0)
        neg = abs(
            sum(float(r["roe_pct"]) for r in recent if float(r.get("roe_pct") or 0) < 0)
        )
        pf = (pos / neg) if neg > 0 else (2.0 if pos > 0 else 1.0)
        streak = 0
        for r in reversed(recent):
            if float(r.get("roe_pct") or 0) < 0:
                streak += 1
            else:
                break
        avg = sum(float(r.get("roe_pct") or 0) for r in recent) / len(recent)
        return round(wr, 4), round(min(5.0, pf), 3), streak, round(avg, 2)

    def _bootstrap_from_profitability(
        self, window_sec: float, limit: int
    ) -> tuple[float, float, int, float]:
        """Seed from profitability.json when roe_learning is cold."""
        path = self.path.parent / "profitability.json"
        if not path.is_file():
            return 0.5, 1.0, 0, 0.0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            trades = raw.get("trades") or []
        except Exception:
            return 0.5, 1.0, 0, 0.0
        cutoff = time.time() - window_sec
        rows = []
        for t in reversed(trades):
            ts = float(t.get("ts") or 0)
            if ts < cutoff and rows:
                break
            roe = t.get("roe_pct")
            if roe is None:
                continue
            rows.append(float(roe))
            if len(rows) >= limit:
                break
        if not rows:
            return 0.5, 1.0, 0, 0.0
        wr = sum(1 for r in rows if r > 0) / len(rows)
        pos = sum(r for r in rows if r > 0)
        neg = abs(sum(r for r in rows if r < 0))
        pf = (pos / neg) if neg > 0 else (2.0 if pos > 0 else 1.0)
        streak = 0
        for r in rows:
            if r < 0:
                streak += 1
            else:
                streak = 0
        return round(wr, 4), round(min(5.0, pf), 3), streak, round(sum(rows) / len(rows), 2)

    def symbol_roe_ema(self, symbol: str) -> float | None:
        row = (self._data.get("symbols") or {}).get(symbol)
        if not row:
            return None
        return float(row.get("roe_ema")) if row.get("roe_ema") is not None else None

    def symbol_score_delta(self, symbol: str) -> float:
        """Small conviction/quality nudge from per-symbol ROE memory."""
        ema = self.symbol_roe_ema(symbol)
        if ema is None:
            return 0.0
        if ema >= 8.0:
            return 0.03
        if ema >= 2.0:
            return 0.01
        if ema <= -15.0:
            return -0.05
        if ema <= -5.0:
            return -0.02
        return 0.0


_store_cache: dict[Path, RoeLearningStore] = {}


def get_roe_store(state_dir: Path) -> RoeLearningStore:
    key = state_dir.resolve()
    if key not in _store_cache:
        _store_cache[key] = RoeLearningStore(key)
    return _store_cache[key]
