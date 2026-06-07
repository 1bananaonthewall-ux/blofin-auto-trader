"""Live dashboard snapshot builder + cache (WebSocket feed)."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from config import load_settings
from exchange_client import BlofinExchange
from mission_config import (
    TARGET_DAILY_GROWTH_PCT,
    progress_toward_daily_goal_pct,
    sole_objective_label,
)

log = logging.getLogger(__name__)

# bot.log can be huge; PICK/CONFLUENCE lines must stay inside the parse window.
_LOG_BOOT_LINES = 8000
_LOG_TAIL_MAX = 8000
_LOG_PARSE_MIN = 8000

# Injected by dashboard_api after module load (avoids circular imports).
_tail_log_lines: Callable[..., tuple[list[str], int]] | None = None
_parse_scan_feed: Callable[..., tuple[list[dict], dict | None]] | None = None
_parse_log_signals: Callable[..., list[dict]] | None = None
_read_json: Callable[..., Any] | None = None
_read_jsonl: Callable[..., list[dict]] | None = None
_load_equity_ticks_raw: Callable[[], list[dict[str, float]]] | None = None
_load_realized_curve: Callable[[], list[dict[str, float]]] | None = None
_baseline_equity_at: Callable[..., float | None] | None = None
_downsample_series: Callable[..., list[dict]] | None = None
_symbol_short: Callable[[str], str] | None = None
_bot_running: Callable[[], bool] | None = None
_equity_range_sec: dict[str, int] = {}
_state_dir: Any = None
_log_file: Any = None


def bind_dashboard_helpers(**kwargs: Any) -> None:
    global _tail_log_lines, _parse_scan_feed, _parse_log_signals
    global _read_json, _read_jsonl, _load_equity_ticks_raw, _load_realized_curve
    global _baseline_equity_at, _downsample_series, _symbol_short, _bot_running
    global _equity_range_sec, _state_dir, _log_file
    for key, val in kwargs.items():
        if key == "equity_range_sec":
            _equity_range_sec = val
        elif key == "state_dir":
            _state_dir = val
        elif key == "log_file":
            _log_file = val
        else:
            globals()[f"_{key}"] = val


def _load_bot_positions() -> tuple[list[dict], float, dict[str, Any]] | None:
    """Rows from bot account_snapshot.json — avoids duplicate exchange polling."""
    from dashboard_publish import load_account_snapshot

    data = load_account_snapshot(_state_dir, max_age_sec=900.0)
    if data is None:
        return None
    rows = list(data.get("positions") or [])
    return rows, float(data.get("updated_at") or 0), data


def _last_known_equity() -> tuple[float, float]:
    """Fallback equity/free from fluid_state or equity_ticks when API is down."""
    fluid = _read_json(_state_dir / "fluid_state.json", {}) or {}
    samples = fluid.get("samples") or []
    for _ts, eq in reversed(samples):
        try:
            val = float(eq)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val, val * 0.85
    ticks_path = _state_dir / "equity_ticks.jsonl"
    if ticks_path.is_file():
        try:
            lines = ticks_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines[-400:]):
                if not line.strip():
                    continue
                row = json.loads(line)
                val = float(row.get("equity") or 0)
                if val > 0:
                    return val, val * 0.85
        except Exception:
            pass
    peak = float(fluid.get("peak_equity") or 0)
    if peak > 0:
        return peak, peak * 0.85
    return 0.0, 0.0


def _position_rows(live: dict[str, dict], registry: dict) -> list[dict]:
    rows: list[dict] = []
    for key, pos in sorted(live.items()):
        sym = str(pos.get("symbol") or key.split("#")[0])
        info = pos.get("info") or {}
        side = str(pos.get("side", "long")).lower()
        entry = float(pos.get("entry_price") or 0)
        mark = float(info.get("markPx") or info.get("markPrice") or pos.get("mark_price") or entry)
        contracts = float(pos.get("contracts") or 0)
        lev = int(pos.get("leverage") or info.get("leverage") or info.get("lever") or 0)
        margin = float(pos.get("margin_usdt") or 0)
        roe_pct, pnl_usd, notional, eff_lev = BlofinExchange.position_display_metrics(
            side=side,
            entry=entry,
            mark=mark,
            margin_usdt=margin,
            leverage=lev,
            unrealized_usd=float(pos.get("unrealized_pnl_usd")) if pos.get("unrealized_pnl_usd") is not None else None,
            row=info if info else None,
            contracts=contracts,
        )
        reg = registry.get(sym, {}) or registry.get(key, {})
        rows.append(
            {
                "position_key": pos.get("position_key") or key,
                "symbol": sym,
                "symbol_short": _symbol_short(sym),
                "side": side,
                "entry": entry,
                "mark": mark,
                "contracts": contracts,
                "leverage": lev,
                "effective_leverage": round(eff_lev, 1) if eff_lev else lev,
                "margin_usdt": round(margin, 4),
                "notional_usdt": round(notional, 4),
                "liquidation_price": float(pos.get("liquidation_price") or 0),
                "pnl_pct": roe_pct,
                "pnl_usd": pnl_usd,
                "conviction": reg.get("conviction"),
                "sl_price": reg.get("sl_price"),
                "tp_price": reg.get("tp_price"),
                "opened_at": reg.get("opened_at"),
                "status": "hold",
            }
        )
    return rows


def _registry_fallback_rows(registry: dict, live_syms: set[str]) -> list[dict]:
    """Show open book from registry when exchange fetch is empty (e.g. 429)."""
    rows: list[dict] = []
    for sym, reg in sorted(registry.items()):
        if sym in live_syms:
            continue
        entry = float(reg.get("entry_price") or 0)
        side = str(reg.get("side", "long")).lower()
        lev = int(reg.get("leverage") or 0)
        rows.append(
            {
                "position_key": sym,
                "symbol": sym,
                "symbol_short": _symbol_short(sym),
                "side": side,
                "entry": entry,
                "mark": entry,
                "contracts": 0.0,
                "leverage": lev,
                "effective_leverage": lev,
                "margin_usdt": 0.0,
                "notional_usdt": 0.0,
                "liquidation_price": 0.0,
                "pnl_pct": 0.0,
                "pnl_usd": 0.0,
                "conviction": reg.get("conviction"),
                "sl_price": reg.get("sl_price"),
                "tp_price": reg.get("tp_price"),
                "opened_at": reg.get("opened_at"),
                "status": "hold",
                "source": "registry",
            }
        )
    return rows


def _enrich_rows_from_exchange(rows: list[dict], live: dict[str, dict]) -> list[dict]:
    """Refresh side/mark/contracts from exchange when snapshot rows are stale."""
    if not live:
        return rows
    by_sym: dict[str, dict] = {}
    for key, pos in live.items():
        sym = str(pos.get("symbol") or key.split("#")[0])
        by_sym[sym] = pos
        by_sym[_norm_symbol(sym)] = pos
    out: list[dict] = []
    for row in rows:
        sym = _norm_symbol(row.get("symbol") or "")
        pos = by_sym.get(sym) or by_sym.get(row.get("symbol") or "")
        if not pos:
            out.append(row)
            continue
        info = pos.get("info") or {}
        side = str(pos.get("side", row.get("side", "long"))).lower()
        entry = float(pos.get("entry_price") or row.get("entry") or 0)
        mark = float(
            pos.get("mark_price")
            or info.get("markPx")
            or info.get("markPrice")
            or row.get("mark")
            or entry
        )
        lev = int(pos.get("leverage") or row.get("leverage") or 0)
        margin = float(pos.get("margin_usdt") or row.get("margin_usdt") or 0)
        contracts = float(pos.get("contracts") or row.get("contracts") or 0)
        roe, pnl, notional, eff = BlofinExchange.position_display_metrics(
            side=side,
            entry=entry,
            mark=mark,
            margin_usdt=margin,
            leverage=lev,
            unrealized_usd=float(pos.get("unrealized_pnl_usd"))
            if pos.get("unrealized_pnl_usd") is not None
            else None,
            row=info if info else None,
            contracts=contracts,
        )
        merged = dict(row)
        merged.update(
            {
                "side": side,
                "entry": entry,
                "mark": mark,
                "contracts": contracts,
                "leverage": lev,
                "effective_leverage": round(eff, 1) if eff else lev,
                "margin_usdt": round(margin, 4),
                "notional_usdt": round(notional, 4),
                "pnl_pct": roe,
                "pnl_usd": pnl,
                "liquidation_price": float(pos.get("liquidation_price") or 0),
            }
        )
        out.append(merged)
    return out


def _prune_stale_position_rows(
    rows: list[dict],
    *,
    registry: dict,
    exchange_syms: set[str] | None,
) -> list[dict]:
    """Drop closed/stale rows (e.g. snapshot still lists TWT after exchange closed it)."""
    if exchange_syms:
        allowed = {_norm_symbol(s) for s in exchange_syms} | set(exchange_syms)
        return [p for p in rows if _norm_symbol(p.get("symbol") or "") in allowed]
    if registry:
        allowed = {_norm_symbol(s) for s in registry.keys()}
        return [p for p in rows if _norm_symbol(p.get("symbol") or "") in allowed]
    return [p for p in rows if float(p.get("contracts") or 0) > 0]


def _norm_symbol(sym: str) -> str:
    s = str(sym or "").strip()
    if not s:
        return ""
    if "/" in s or s.endswith(":USDT"):
        return s
    return f"{s}/USDT:USDT"


def _signal_row_from_pick(pick: dict, registry: dict) -> dict:
    sym = pick["symbol"]
    conf_pct = float(pick.get("confluence_pct") or 0)
    conf = float(pick.get("confidence") or (conf_pct / 100.0 if conf_pct else 0))
    row = {
        "symbol": sym,
        "side": pick.get("side", "long"),
        "score": float(pick.get("score") or pick.get("pick_pct") or 0),
        "confidence": conf,
        "leverage": int(pick.get("leverage") or 0),
        "confluence_pct": pick.get("confluence_pct"),
        "pick_pct": pick.get("pick_pct"),
        "fast_pct": pick.get("fast_pct"),
        "tier": pick.get("tier"),
        "rank": float(pick.get("rank") or 0),
        "zone": pick.get("zone"),
    }
    key = sym if ("/" in sym) else f"{sym}/USDT:USDT"
    if key in registry:
        row["conviction"] = registry[key].get("conviction")
    return row


def _signals_from_log(lines: list[str], held_symbols: set[str] | None = None) -> dict[str, list[dict]]:
    held = {_norm_symbol(s) for s in (held_symbols or set())}
    registry = _read_json(_state_dir / "position_registry.json", {}) or {}
    scan_picks, _ = _parse_scan_feed(lines)
    ml_parsed = _parse_log_signals(lines[-400:])
    ranked: list[dict] = []
    seen: set[str] = set()
    for pick in scan_picks:
        sym = pick["symbol"]
        key = _norm_symbol(sym)
        if key in held:
            continue
        seen.add(sym)
        seen.add(key)
        ranked.append(_signal_row_from_pick(pick, registry))
    for s in ml_parsed:
        sym = s["symbol"]
        key = _norm_symbol(sym)
        if key in held or sym in held:
            continue
        if sym in seen or key in seen:
            continue
        seen.add(sym)
        seen.add(key)
        score = float(s.get("score") or 0)
        if score <= 1.5:
            score *= 100.0
        s["score"] = score
        s["rank"] = score
        reg_key = sym if ("/" in sym) else f"{sym}/USDT:USDT"
        if reg_key in registry:
            s["conviction"] = registry[reg_key].get("conviction")
        ranked.append(s)
    ranked.sort(key=lambda x: float(x.get("rank") or x.get("score") or 0), reverse=True)
    active = ranked[:6]
    active_syms = {s["symbol"] for s in active}
    developing = [s for s in ranked if s["symbol"] not in active_syms][:24]
    return {
        "active_setups": active,
        "developing_setups": developing,
        "recent_scan_count": len(ranked),
    }


def parse_ts(raw: Any) -> float:
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            return val / 1000.0
        return val
    text = str(raw).strip()
    try:
        val = float(text)
        if val > 1e12:
            return val / 1000.0
        return val
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _journal_margin_before(symbol: str, close_ts: float) -> float:
    """Last open margin from trades.jsonl before a close timestamp."""
    path = _state_dir / "trades.jsonl"
    if not _read_jsonl or not path.is_file():
        return 0.0
    best = 0.0
    for raw in _read_jsonl(path, limit=400):
        if str(raw.get("event") or "").lower() != "open":
            continue
        if str(raw.get("symbol") or "") != symbol:
            continue
        ts = parse_ts(raw.get("ts"))
        if close_ts > 0 and ts > close_ts:
            continue
        margin = float(raw.get("margin") or 0)
        if margin > 0 and ts >= best:
            best = ts
            best_margin = margin
    return float(locals().get("best_margin", 0) or 0)


def trades_stream_version() -> float:
    """Bump when any trade journal file changes (for WS dedup + cache invalidation)."""
    mtimes: list[float] = []
    for name in (
        "profitability.json",
        "trade_outcomes.jsonl",
        "trades.jsonl",
        "roe_learning.json",
        "dashboard_closes.jsonl",
    ):
        path = _state_dir / name
        if path.is_file():
            mtimes.append(path.stat().st_mtime)
    return max(mtimes) if mtimes else 0.0


def positions_stream_version() -> float:
    """Bump when open-book snapshot files change."""
    mtimes: list[float] = []
    for name in ("account_snapshot.json", "live_positions.json", "position_registry.json"):
        path = _state_dir / name
        if path.is_file():
            mtimes.append(path.stat().st_mtime)
    return max(mtimes) if mtimes else 0.0


def signals_stream_version() -> float:
    """Bump when scan/setup inputs change."""
    mtimes: list[float] = []
    for name in ("position_registry.json", "markets_cache.json", "hourly_report.json"):
        path = _state_dir / name
        if path.is_file():
            mtimes.append(path.stat().st_mtime)
    if _log_file and _log_file.is_file():
        mtimes.append(_log_file.stat().st_mtime)
    return max(mtimes) if mtimes else 0.0


def _log_lines_for_parse(log_lines: list[str] | None = None) -> list[str]:
    if log_lines and len(log_lines) >= _LOG_PARSE_MIN:
        return list(log_lines)
    if _tail_log_lines:
        lines, _ = _tail_log_lines(_LOG_PARSE_MIN)
        if lines:
            return lines
    return list(log_lines or [])


def build_live_positions(
    *,
    exchange_positions: dict[str, dict] | None = None,
    exchange_positions_ts: float = 0.0,
    now: float | None = None,
) -> list[dict]:
    """Fresh open positions — exchange book preferred, snapshot+registry fallback."""
    now = now or time.time()
    registry = _read_json(_state_dir / "position_registry.json", {}) or {}
    exchange_fresh = bool(
        exchange_positions and (now - exchange_positions_ts) < 120.0
    )
    exchange_syms: set[str] | None = None
    if exchange_fresh and exchange_positions:
        exchange_syms = {
            str(p.get("symbol") or k.split("#")[0])
            for k, p in exchange_positions.items()
        }
        return _position_rows(exchange_positions, registry)

    bot_book = _load_bot_positions()
    if bot_book is not None:
        positions, _, _meta = bot_book
        refreshed: list[dict] = []
        for p in positions:
            side = str(p.get("side", "long")).lower()
            entry = float(p.get("entry") or 0)
            mark = float(p.get("mark") or entry)
            lev = int(p.get("leverage") or 0)
            margin = float(p.get("margin_usdt") or 0)
            roe, pnl, notional, eff = BlofinExchange.position_display_metrics(
                side=side,
                entry=entry,
                mark=mark,
                margin_usdt=margin,
                leverage=lev,
                unrealized_usd=float(p.get("pnl_usd")) if p.get("pnl_usd") is not None else None,
                contracts=float(p.get("contracts") or 0),
            )
            row = dict(p)
            row["pnl_pct"] = roe
            row["pnl_usd"] = pnl
            row["notional_usdt"] = notional
            row["effective_leverage"] = round(eff, 1) if eff else lev
            refreshed.append(row)
        positions = _prune_stale_position_rows(
            refreshed, registry=registry, exchange_syms=exchange_syms
        )
        if exchange_positions:
            positions = _enrich_rows_from_exchange(positions, exchange_positions)
        return positions

    if exchange_positions:
        positions = _position_rows(exchange_positions, registry)
        live_syms = {p["symbol"] for p in positions}
        if len(positions) < len(registry):
            positions = positions + _registry_fallback_rows(registry, live_syms)
            positions.sort(key=lambda x: x["symbol"])
        return positions
    return []


def build_signals_feed(
    *,
    positions: list[dict] | None = None,
    log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Fresh active + developing setups from bot.log scan feed."""
    registry = _read_json(_state_dir / "position_registry.json", {}) or {}
    lines = _log_lines_for_parse(log_lines)
    if positions is None:
        positions = build_live_positions()
    live_syms = {p.get("symbol") for p in positions if p.get("symbol")}
    held = live_syms | set(registry.keys())
    return _signals_from_log(lines, held)


def _append_dashboard_close(row: dict[str, Any]) -> None:
    """Persist exchange-detected closes for dashboard when bot misses a label pass."""
    path = _state_dir / "dashboard_closes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    sym = str(row.get("symbol") or "")
    ts = float(row.get("ts") or 0)
    if sym and ts > 0 and path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-80:]:
                if not line.strip():
                    continue
                prev = json.loads(line)
                if (
                    str(prev.get("symbol") or "") == sym
                    and abs(float(prev.get("ts") or 0) - ts) < 120
                ):
                    return
        except Exception:
            pass
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


from roe_learning import default_close_leverage as _default_close_leverage
from roe_learning import journal_open_before as _journal_open_before
from roe_learning import resolve_close_pnl_roe as _resolve_close_pnl_roe

_CT_CACHE: dict[str, float] = {}


def _contract_size_for(symbol: str) -> float:
    if symbol in _CT_CACHE:
        return _CT_CACHE[symbol]
    ct = 1.0
    raw = _read_json(_state_dir / "markets_cache.json", None)
    markets = raw if isinstance(raw, list) else (raw or {}).get("markets") if isinstance(raw, dict) else []
    if isinstance(markets, list):
        for m in markets:
            if str(m.get("symbol") or "") == symbol:
                ct = float(m.get("contract_size") or 1.0)
                break
    _CT_CACHE[symbol] = ct
    return ct


def _prof_pnl_near(prof_rows: list[dict], symbol: str, close_ts: float, window_sec: float = 120.0) -> float | None:
    best: float | None = None
    best_dt = window_sec + 1.0
    for t in prof_rows:
        if str(t.get("symbol") or "") != symbol:
            continue
        ts = parse_ts(t.get("ts"))
        if ts <= 0 or close_ts <= 0:
            continue
        dt = abs(ts - close_ts)
        if dt <= window_sec and dt < best_dt:
            best_dt = dt
            best = float(t.get("net_pnl") or t.get("pnl_usd") or 0)
    return best


def _format_closed_row(
    *,
    symbol: str,
    side: str,
    pnl_usd: float,
    ts: float,
    event: str,
    entry: float | None = None,
    exit_px: float | None = None,
    leverage: int | None = None,
    margin_usdt: float | None = None,
    roe_pct: float | None = None,
    source: str,
) -> dict[str, Any]:
    entry_v = float(entry or 0)
    exit_v = float(exit_px or 0)
    pnl_pct: float | None = None
    if entry_v > 0 and exit_v > 0:
        if side.lower() == "long":
            pnl_pct = round((exit_v - entry_v) / entry_v * 100.0, 2)
        else:
            pnl_pct = round((entry_v - exit_v) / entry_v * 100.0, 2)
    if roe_pct is None and float(margin_usdt or 0) > 0:
        roe_pct = round(float(pnl_usd) / float(margin_usdt) * 100.0, 2)
    closed_at = (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts > 0 else None
    )
    return {
        "symbol": symbol,
        "symbol_short": _symbol_short(symbol),
        "side": side,
        "pnl_usd": round(pnl_usd, 6),
        "pnl_pct": pnl_pct,
        "roe_pct": roe_pct,
        "event": event,
        "entry": round(entry_v, 8) if entry_v > 0 else None,
        "exit": round(exit_v, 8) if exit_v > 0 else None,
        "leverage": leverage,
        "ts": ts,
        "closed_at": closed_at,
        "source": source,
    }


def closed_trades_list(limit: int = 80, hours: float = 0) -> list[dict]:
    """Recent realized closes — profitability.json + trade_outcomes.jsonl merged."""
    registry = _read_json(_state_dir / "position_registry.json", {}) or {}
    prof = _read_json(_state_dir / "profitability.json", {"trades": []}) or {"trades": []}
    prof_rows = list(prof.get("trades") or [])
    cutoff = time.time() - hours * 3600 if hours > 0 else 0

    rows: list[dict] = []

    def _append_row(
        *,
        sym: str,
        side: str,
        ts: float,
        entry: float,
        exit_px: float,
        event: str,
        source: str,
        fill_pnl: float | None,
        prof_pnl: float | None,
        margin: float,
        contracts: float,
        lev: int,
    ) -> None:
        if cutoff and ts and ts < cutoff:
            return
        if entry <= 0 or exit_px <= 0:
            return
        if margin <= 0 or contracts <= 0:
            j_margin, j_contracts, j_lev = _journal_open_before(_state_dir, sym, ts)
            if margin <= 0 and j_margin > 0:
                margin = j_margin
            if contracts <= 0 and j_contracts > 0:
                contracts = j_contracts
            if lev <= 0 and j_lev > 0:
                lev = j_lev
        if margin <= 0:
            margin = _journal_margin_before(sym, ts)
        pnl, roe = _resolve_close_pnl_roe(
            side=side,
            entry=entry,
            exit_px=exit_px,
            fill_pnl=fill_pnl,
            prof_pnl=prof_pnl,
            margin_usdt=margin if margin > 0 else None,
            leverage=lev if lev > 0 else _default_close_leverage(),
            contracts=contracts if contracts > 0 else None,
            contract_size=_contract_size_for(sym),
        )
        rows.append(
            _format_closed_row(
                symbol=sym,
                side=side,
                pnl_usd=pnl,
                roe_pct=roe,
                ts=ts,
                event=event,
                entry=entry,
                exit_px=exit_px,
                leverage=lev if lev > 0 else None,
                margin_usdt=margin if margin > 0 else None,
                source=source,
            )
        )

    roe_state = _read_json(_state_dir / "roe_learning.json", {}) or {}
    for raw in reversed(list((roe_state.get("global") or {}).get("recent") or [])):
        sym = str(raw.get("symbol") or "?")
        ts = parse_ts(raw.get("ts"))
        if cutoff and ts and ts < cutoff:
            continue
        if any(
            r.get("symbol") == sym and abs(float(r.get("ts") or 0) - ts) < 300
            for r in rows
        ):
            continue
        rows.append(
            _format_closed_row(
                symbol=sym,
                side=str(raw.get("side") or "long"),
                pnl_usd=float(raw.get("pnl_usd") or 0),
                roe_pct=float(raw.get("roe_pct") or 0) if raw.get("roe_pct") is not None else None,
                ts=ts,
                event=str(raw.get("event") or "close"),
                source="roe_learning",
            )
        )

    if _read_jsonl is not None:
        journal_path = _state_dir / "trades.jsonl"
        for raw in _read_jsonl(journal_path, limit=max(limit * 4, 200)):
            if str(raw.get("event") or "").lower() != "close":
                continue
            sym = str(raw.get("symbol") or "?")
            ts = parse_ts(raw.get("ts"))
            entry = float(raw.get("entry") or 0)
            exit_px = float(raw.get("exit") or raw.get("exit_px") or 0)
            if cutoff and ts and ts < cutoff:
                continue
            if any(
                r.get("symbol") == sym and abs(float(r.get("ts") or 0) - ts) < 300
                for r in rows
            ):
                continue
            margin = float(raw.get("margin") or raw.get("margin_usdt") or 0)
            lev = int(raw.get("leverage") or 0) or _default_close_leverage()
            contracts = float(raw.get("contracts") or 0)
            pnl = float(raw.get("pnl_usd") or 0)
            roe = raw.get("roe_pct")
            if roe is not None:
                try:
                    roe = float(roe)
                except (TypeError, ValueError):
                    roe = None
            if entry > 0 and exit_px > 0:
                pnl_v, roe_v = _resolve_close_pnl_roe(
                    side=str(raw.get("side") or "long"),
                    entry=entry,
                    exit_px=exit_px,
                    prof_pnl=pnl if pnl else None,
                    margin_usdt=margin if margin > 0 else None,
                    leverage=lev,
                    contracts=contracts if contracts > 0 else None,
                    contract_size=_contract_size_for(sym),
                )
                pnl = pnl_v
                roe = roe_v if roe is None else roe
            rows.append(
                _format_closed_row(
                    symbol=sym,
                    side=str(raw.get("side") or "long"),
                    pnl_usd=pnl,
                    roe_pct=roe,
                    ts=ts,
                    event=str(raw.get("reason") or raw.get("event") or "close"),
                    entry=entry if entry > 0 else None,
                    exit_px=exit_px if exit_px > 0 else None,
                    leverage=lev if lev > 0 else None,
                    margin_usdt=margin if margin > 0 else None,
                    source="journal",
                )
            )

        dash_path = _state_dir / "dashboard_closes.jsonl"
        for raw in _read_jsonl(dash_path, limit=max(limit * 4, 120)):
            sym = str(raw.get("symbol") or "?")
            ts = parse_ts(raw.get("ts") or raw.get("close_ts"))
            entry = float(raw.get("entry") or raw.get("entry_price") or 0)
            exit_px = float(raw.get("exit") or raw.get("close_price") or 0)
            if cutoff and ts and ts < cutoff:
                continue
            if any(
                r.get("symbol") == sym and abs(float(r.get("ts") or 0) - ts) < 300
                for r in rows
            ):
                continue
            _append_row(
                sym=sym,
                side=str(raw.get("side") or "long"),
                ts=ts,
                entry=entry,
                exit_px=exit_px,
                event=str(raw.get("reason") or raw.get("event") or "exchange_close"),
                source="dashboard",
                fill_pnl=float(raw.get("fill_pnl")) if raw.get("fill_pnl") is not None else None,
                prof_pnl=None,
                margin=float(raw.get("margin_usdt") or 0),
                contracts=float(raw.get("contracts") or 0),
                lev=int(raw.get("leverage") or 0) or _default_close_leverage(),
            )

        outcomes_path = _state_dir / "trade_outcomes.jsonl"
        for raw in _read_jsonl(outcomes_path, limit=max(limit * 4, 160)):
            if str(raw.get("event") or "").lower() != "outcome":
                continue
            sym = str(raw.get("symbol") or "?")
            ts = parse_ts(raw.get("close_ts") or raw.get("ts"))
            entry = float(raw.get("entry_price") or 0)
            exit_px = float(raw.get("close_price") or 0)
            reg = registry.get(sym) or {}
            lev = int(raw.get("leverage") or reg.get("leverage") or 0) or _default_close_leverage()
            margin = float(raw.get("margin_usdt") or reg.get("margin_usdt") or 0)
            contracts = float(raw.get("contracts") or reg.get("contracts") or 0)
            fill_pnl = raw.get("fill_pnl")
            if fill_pnl is not None:
                try:
                    fill_pnl = float(fill_pnl)
                except (TypeError, ValueError):
                    fill_pnl = None
            _append_row(
                sym=sym,
                side=str(raw.get("side") or "long"),
                ts=ts,
                entry=entry,
                exit_px=exit_px,
                event=str(raw.get("reason") or raw.get("outcome") or "close"),
                source="outcome",
                fill_pnl=fill_pnl,
                prof_pnl=_prof_pnl_near(prof_rows, sym, ts, window_sec=300.0),
                margin=margin,
                contracts=contracts,
                lev=lev,
            )

    for t in prof_rows:
        sym = str(t.get("symbol") or "?")
        ts = parse_ts(t.get("ts"))
        entry_px = float(t.get("entry") or 0)
        exit_px = float(t.get("exit") or 0)
        if entry_px <= 0 or exit_px <= 0:
            continue
        if any(
            r.get("symbol") == sym and abs(float(r.get("ts") or 0) - ts) < 300
            for r in rows
        ):
            continue
        reg = registry.get(sym) or {}
        lev = int(t.get("leverage") or reg.get("leverage") or 0) or _default_close_leverage()
        margin = float(reg.get("margin_usdt") or 0)
        contracts = float(reg.get("contracts") or 0)
        _append_row(
            sym=sym,
            side=str(t.get("side") or "long"),
            ts=ts,
            entry=entry_px,
            exit_px=exit_px,
            event=str(t.get("event") or "close"),
            source="profitability",
            fill_pnl=None,
            prof_pnl=float(t.get("net_pnl") or t.get("pnl_usd") or 0),
            margin=margin,
            contracts=contracts,
            lev=lev,
        )

    deduped: list[dict] = []
    for row in sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True):
        sym = row.get("symbol")
        dup = next(
            (
                d
                for d in deduped
                if d.get("symbol") == sym
                and abs(float(d.get("ts") or 0) - float(row.get("ts") or 0)) < 300
            ),
            None,
        )
        if dup is None:
            deduped.append(row)
            continue
        if abs(float(row.get("pnl_usd") or 0)) < abs(float(dup.get("pnl_usd") or 0)) * 1.35:
            deduped[deduped.index(dup)] = row
    return deduped[:limit]


# Back-compat alias for internal callers
_closed_trades_list = closed_trades_list


def _pnl_curve_payload(
    eq_range: str = "ALL",
    limit: int = 800,
    *,
    live_equity: float | None = None,
) -> dict[str, Any]:
    from dashboard_api import build_pnl_curve_payload

    return build_pnl_curve_payload(
        eq_range=eq_range, limit=limit, live_equity=live_equity
    )


class LiveDataHub:
    """Background cache refreshed from logs + exchange (rate-limit aware)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = {"stream_ts": None, "errors": {}}
        self._log_offset = 0
        self._log_tail: list[str] = []
        self._ex: BlofinExchange | None = None
        self._ex_ts = 0.0
        self._positions: dict[str, dict] = {}
        self._positions_ts = 0.0
        self._exchange_positions_ts = 0.0
        self._equity = 0.0
        self._free = 0.0
        self._equity_ts = 0.0
        self._equity_err: str | None = None
        self._positions_err: str | None = None
        self._exchange_backoff_until = 0.0
        self._live_positions_mtime = 0.0
        self._profitability_mtime = 0.0
        self._trades_journal_mtime = 0.0
        self._trades_outcomes_mtime = 0.0
        self._roe_learning_mtime = 0.0
        self._dashboard_closes_mtime = 0.0
        self._prev_open_syms: set[str] = set()
        self._closed_cache: list[dict] | None = None
        self._closed_cache_ver = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            from position_registry import PositionRegistry

            self._prev_open_syms = set(PositionRegistry(_state_dir).keys())
        except Exception:
            self._prev_open_syms = set()
        try:
            now = time.time()
            self._refresh_exchange(now)
            snap = self._compose(now, self._refresh_logs())
            with self._lock:
                self._snapshot = snap
        except Exception as exc:
            log.warning("live hub bootstrap failed: %s", exc)
        self._thread = threading.Thread(target=self._loop, name="dashboard-live", daemon=True)
        self._thread.start()
        log.info("dashboard live hub started")

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def get_fresh_positions(self) -> tuple[list[dict], dict[str, str]]:
        with self._lock:
            now = time.time()
            if now - self._exchange_positions_ts >= 10.0 or not self._positions:
                self._refresh_exchange(now)
            rows = build_live_positions(
                exchange_positions=self._positions,
                exchange_positions_ts=self._exchange_positions_ts,
                now=now,
            )
            errors: dict[str, str] = {}
            if self._positions_err:
                errors["positions"] = self._positions_err
            return rows, errors

    def get_fresh_signals(
        self, positions: list[dict] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            if positions is None:
                positions, _ = self.get_fresh_positions()
            return build_signals_feed(positions=positions, log_lines=self._lines_for_parse())

    def _get_closed_trades(self) -> list[dict]:
        ver = trades_stream_version()
        if self._closed_cache is not None and ver == self._closed_cache_ver:
            return self._closed_cache
        rows = closed_trades_list(limit=80)
        self._closed_cache = rows
        self._closed_cache_ver = ver
        return rows

    def _get_exchange(self) -> BlofinExchange | None:
        if self._ex is not None:
            return self._ex
        if time.time() < self._exchange_backoff_until:
            return None
        try:
            settings = load_settings()
            ex = BlofinExchange(settings)
            ex.load()
            self._ex = ex
            self._ex_ts = time.time()
            return ex
        except Exception as exc:
            self._exchange_backoff_until = time.time() + 90
            log.debug("exchange load failed (backoff 90s): %s", exc)
            return None

    def _refresh_exchange(self, now: float) -> None:
        # Equity from bot snapshot when available; positions always reconciled vs exchange.
        snap = _load_bot_positions()
        if snap is not None:
            _rows, bot_ts, meta = snap
            eq = float(meta.get("equity") or 0)
            fr = float(meta.get("free_margin") or 0)
            if eq > 0:
                self._equity = eq
                self._free = fr if fr > 0 else eq * 0.85
                self._equity_ts = bot_ts
                self._equity_err = None
            elif not meta.get("api_ok", True):
                fb_eq, fb_free = _last_known_equity()
                if fb_eq > 0:
                    self._equity = fb_eq
                    self._free = fb_free
                    self._equity_err = "Using last known equity (Blofin rate limit)"
            if not meta.get("api_ok", True):
                self._positions_err = "Using cached positions (Blofin rate limit)"

        need_positions = now - self._exchange_positions_ts >= 15.0 or not self._positions
        need_equity = snap is None and (now - self._equity_ts >= 60.0 or self._equity <= 0)
        if not need_positions and not need_equity:
            return
        ex = self._get_exchange()
        if ex is None:
            return
        if need_positions:
            try:
                fresh = ex.fetch_all_positions()
                if fresh:
                    self._detect_closed_positions(fresh, ex)
                    self._positions = fresh
                    self._exchange_positions_ts = now
                    self._positions_ts = now
                    self._positions_err = None
                    self._reconcile_positions_file(fresh)
                elif self._positions:
                    self._positions_err = (
                        self._positions_err or "exchange positions empty (rate limit?); using cache"
                    )[:200]
                else:
                    self._positions = {}
                    self._exchange_positions_ts = 0.0
                    self._positions_ts = now
                    self._positions_err = None
            except Exception as exc:
                msg = str(exc)[:200]
                self._positions_err = msg
                if "429" in msg:
                    self._exchange_backoff_until = time.time() + 60.0
                log.debug("live positions refresh failed: %s", exc)
        if need_equity:
            try:
                eq = ex.fetch_equity_usdt()
                fr = ex.fetch_free_equity_usdt()
                if eq > 0:
                    self._equity = eq
                    self._free = fr
                    self._equity_ts = now
                    self._equity_err = None
                elif self._equity <= 0:
                    self._equity_err = (self._equity_err or "equity fetch empty (rate limit?)")[:200]
            except Exception as exc:
                msg = str(exc)[:200]
                self._equity_err = msg
                if "429" in msg:
                    self._exchange_backoff_until = time.time() + 60.0
                log.debug("live equity refresh failed: %s", exc)

    def _detect_closed_positions(
        self, positions: dict[str, dict], ex: BlofinExchange
    ) -> None:
        """When a symbol vanishes from the exchange book, record a dashboard close row."""
        new_syms = {
            str(p.get("symbol") or k.split("#")[0]) for k, p in positions.items()
        }
        if not self._prev_open_syms:
            self._prev_open_syms = new_syms
            return
        vanished = self._prev_open_syms - new_syms
        self._prev_open_syms = new_syms
        if not vanished:
            return
        try:
            from position_registry import PositionRegistry

            registry = PositionRegistry(_state_dir)
        except Exception:
            return
        for sym in vanished:
            meta = registry.get(sym) or {}
            side = str(meta.get("side") or "long")
            entry = float(meta.get("entry_price") or 0)
            opened_at = float(meta.get("opened_at") or 0) or None
            lev = int(meta.get("leverage") or 0) or _default_close_leverage()
            margin = float(meta.get("margin_usdt") or 0)
            contracts = float(meta.get("contracts") or 0)
            close_px = 0.0
            fill_pnl: float | None = None
            reason = "exchange_close"
            try:
                fill = ex.fetch_recent_close_fill(sym, side, opened_at=opened_at)
            except Exception:
                fill = None
            if fill and float(fill.get("fill_price") or 0) > 0:
                close_px = float(fill["fill_price"])
                raw_pnl = fill.get("fill_pnl")
                if raw_pnl is not None:
                    try:
                        fill_pnl = float(raw_pnl)
                    except (TypeError, ValueError):
                        fill_pnl = None
                reason = str(fill.get("reason") or "exchange_close")
            if close_px <= 0 and entry > 0:
                if getattr(ex, "stream", None):
                    close_px = float(ex.stream.get_last_price(sym) or 0)
            if close_px <= 0:
                close_px = entry
            ts = time.time()
            pnl_usd = fill_pnl
            roe_pct = None
            if pnl_usd is None and entry > 0 and close_px > 0:
                pnl_usd, roe_pct = _resolve_close_pnl_roe(
                    side=side,
                    entry=entry,
                    exit_px=close_px,
                    margin_usdt=margin if margin > 0 else None,
                    leverage=lev,
                    contracts=contracts if contracts > 0 else None,
                    contract_size=_contract_size_for(sym),
                )
            _append_dashboard_close(
                {
                    "symbol": sym,
                    "side": side,
                    "entry": entry,
                    "exit": close_px,
                    "close_price": close_px,
                    "ts": ts,
                    "reason": reason,
                    "fill_pnl": fill_pnl,
                    "pnl_usd": pnl_usd,
                    "roe_pct": roe_pct,
                    "leverage": lev,
                    "margin_usdt": margin,
                    "contracts": contracts,
                }
            )
            self._closed_cache = None
            log.info("dashboard recorded exchange close %s %s", sym, side)

    def _reconcile_positions_file(self, positions: dict[str, dict]) -> None:
        """Rewrite account_snapshot + registry when exchange book differs from disk."""
        try:
            from dashboard_publish import reconcile_account_snapshot
            from position_registry import PositionRegistry

            registry = PositionRegistry(_state_dir)
            eq = self._equity if self._equity > 0 else 0.0
            fr = self._free if self._free > 0 else eq * 0.85
            if reconcile_account_snapshot(
                _state_dir,
                positions,
                registry,
                equity=eq,
                free_margin=fr,
                api_ok=True,
            ):
                log.info(
                    "dashboard auto-fixed positions snapshot (%d open)",
                    len(positions),
                )
        except Exception as exc:
            log.debug("position reconcile skipped: %s", exc)

    def _refresh_logs(self) -> list[str]:
        if not _log_file.is_file():
            return []
        size = _log_file.stat().st_size
        if size < self._log_offset:
            self._log_offset = 0
            self._log_tail = []
        if self._log_offset >= size:
            return []
        if self._log_offset == 0:
            lines, _ = _tail_log_lines(_LOG_BOOT_LINES)
            self._log_tail = lines[-_LOG_TAIL_MAX:]
            self._log_offset = size
            return list(self._log_tail)
        with _log_file.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._log_offset)
            chunk = fh.read()
        self._log_offset = size
        if not chunk:
            return []
        parts = chunk.splitlines()
        if not chunk.endswith("\n") and parts:
            self._log_offset -= len(parts[-1].encode("utf-8", errors="replace"))
            parts = parts[:-1]
        if parts:
            self._log_tail = (self._log_tail + parts)[-_LOG_TAIL_MAX:]
        return parts

    def _lines_for_parse(self) -> list[str]:
        if len(self._log_tail) >= _LOG_PARSE_MIN:
            return list(self._log_tail)
        lines, _ = _tail_log_lines(_LOG_PARSE_MIN)
        return lines if lines else list(self._log_tail)

    def _compose(self, now: float, log_delta: list[str]) -> dict[str, Any]:
        settings = load_settings()
        lines = self._lines_for_parse()
        picks, scan_plan = _parse_scan_feed(lines)
        account_meta: dict[str, Any] = {}
        bot_book = _load_bot_positions()
        if bot_book is not None:
            _, _, account_meta = bot_book

        positions = build_live_positions(
            exchange_positions=self._positions,
            exchange_positions_ts=self._exchange_positions_ts,
            now=now,
        )
        signals = build_signals_feed(positions=positions, log_lines=lines)
        exposure = sum(float(p.get("notional_usdt") or 0) for p in positions)
        unrealized = sum(float(p.get("pnl_usd") or 0) for p in positions)
        equity = self._equity
        free = self._free
        if bot_book is not None:
            eq_snap = float(account_meta.get("equity") or 0)
            fr_snap = float(account_meta.get("free_margin") or 0)
            if eq_snap > 0:
                equity = eq_snap
                free = fr_snap if fr_snap > 0 else eq_snap * 0.85
        if equity <= 0:
            fb_eq, fb_free = _last_known_equity()
            if fb_eq > 0:
                equity = fb_eq
                free = fb_free
        if equity > 0:
            from equity_ticks import append_equity_tick

            append_equity_tick(
                _state_dir,
                equity,
                min_interval_sec=8.0,
                api_ok=bool(account_meta.get("api_ok", True)),
            )
        pnl_curve_state = _read_json(_state_dir / "pnl_curve.json", {})
        fluid = _read_json(_state_dir / "fluid_state.json", {})
        hourly = _read_json(_state_dir / "hourly_report.json", {})
        all_ticks = _load_equity_ticks_raw(
            live_equity=equity if equity > 0 else None
        )
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        day_base = _baseline_equity_at(all_ticks, day_start)
        month_base = _baseline_equity_at(all_ticks, month_start)
        session_base = _baseline_equity_at(all_ticks, time.time() - 86400.0)
        today_pct = (
            (equity / day_base - 1.0) * 100.0 if day_base and day_base > 0 and equity > 0 else 0.0
        )
        progress = progress_toward_daily_goal_pct(today_pct)

        errors: dict[str, str] = {}
        if self._equity_err:
            errors["equity"] = self._equity_err
        if self._positions_err:
            errors["positions"] = self._positions_err
        if not account_meta.get("api_ok", True):
            errors["api"] = "Blofin rate limit — showing last known account data"

        return {
            "stream_ts": datetime.now(timezone.utc).isoformat(),
            "status": {
                "mission": sole_objective_label(),
                "target_daily_growth_pct": TARGET_DAILY_GROWTH_PCT,
                "today_growth_pct": round(today_pct, 4),
                "progress_today_pct": round(progress, 4),
                "progress_log_pct": round(progress, 4),
                "equity": round(equity, 4),
                "free_margin": round(free, 4),
                "used_margin": round(max(equity - free, 0), 4),
                "exposure_usdt": round(exposure, 4),
                "unrealized_pnl": round(unrealized, 4),
                "daily_pnl": round(equity - day_base, 4) if day_base is not None else None,
                "monthly_pnl": round(equity - month_base, 4) if month_base is not None else None,
                "session_pnl": round(equity - session_base, 4) if session_base is not None else None,
                "open_count": len(positions),
                "bot_running": _bot_running(),
                "live": settings.mode == "live" and not settings.dry_run,
                "mode": settings.mode,
                "dry_run": settings.dry_run,
                "curve_phase": pnl_curve_state.get("last_phase"),
                "verticality": pnl_curve_state.get("last_verticality"),
                "peak_equity": fluid.get("peak_equity"),
                "hourly": hourly.get("tuning", {}),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "positions": positions,
            "active_setups": signals["active_setups"],
            "developing_setups": signals["developing_setups"],
            "closed": self._get_closed_trades(),
            "closed_updated_at": datetime.now(timezone.utc).isoformat(),
            "trades_version": self._closed_cache_ver,
            "positions_version": positions_stream_version(),
            "signals_version": signals_stream_version(),
            "scanner": {
                "picks": picks[:64],
                "count": len(picks),
                "scan_plan": scan_plan,
                "source": "bot.log",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "pnl_curve": _pnl_curve_payload(
                eq_range="ALL",
                limit=800,
                live_equity=equity if equity > 0 else None,
            ),
            "log_lines": log_delta if log_delta else [],
            "log_tail": list(self._log_tail[-500:]),
            "log_offset": self._log_offset,
            "errors": errors,
        }

    def _loop(self) -> None:
        while self._running:
            now = time.time()
            try:
                lp = _state_dir / "live_positions.json"
                if lp.is_file():
                    m = lp.stat().st_mtime
                    if m != self._live_positions_mtime:
                        self._live_positions_mtime = m
                prof = _state_dir / "profitability.json"
                if prof.is_file():
                    pm = prof.stat().st_mtime
                    if pm != self._profitability_mtime:
                        self._profitability_mtime = pm
                        self._closed_cache = None
                journal = _state_dir / "trades.jsonl"
                if journal.is_file():
                    jm = journal.stat().st_mtime
                    if jm != self._trades_journal_mtime:
                        self._trades_journal_mtime = jm
                        self._closed_cache = None
                outcomes = _state_dir / "trade_outcomes.jsonl"
                if outcomes.is_file():
                    om = outcomes.stat().st_mtime
                    if om != self._trades_outcomes_mtime:
                        self._trades_outcomes_mtime = om
                        self._closed_cache = None
                roe_path = _state_dir / "roe_learning.json"
                if roe_path.is_file():
                    rm = roe_path.stat().st_mtime
                    if rm != self._roe_learning_mtime:
                        self._roe_learning_mtime = rm
                        self._closed_cache = None
                dash_closes = _state_dir / "dashboard_closes.jsonl"
                if dash_closes.is_file():
                    dm = dash_closes.stat().st_mtime
                    if dm != self._dashboard_closes_mtime:
                        self._dashboard_closes_mtime = dm
                        self._closed_cache = None
                self._refresh_exchange(now)
                log_delta = self._refresh_logs()
                snap = self._compose(now, log_delta)
                with self._lock:
                    self._snapshot = snap
            except Exception as exc:
                log.warning("live hub tick failed: %s", exc)
            time.sleep(1.5)


_HUB: LiveDataHub | None = None


def get_live_hub() -> LiveDataHub:
    global _HUB
    if _HUB is None:
        _HUB = LiveDataHub()
        _HUB.start()
    return _HUB
