"""Publish live account snapshot for dashboard (written by bot each cycle)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from exchange_client import BlofinExchange
from position_registry import PositionRegistry

ACCOUNT_SNAPSHOT = "account_snapshot.json"
LIVE_POSITIONS = "live_positions.json"


def _symbol_short(symbol: str) -> str:
    return str(symbol).split("/")[0] or str(symbol)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_position_rows(positions: dict[str, dict], registry: PositionRegistry) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, pos in sorted(positions.items()):
        sym = str(pos.get("symbol") or key.split("#")[0])
        info = pos.get("info") or {}
        side = str(pos.get("side", "long")).lower()
        entry = float(pos.get("entry_price") or 0)
        mark = float(
            pos.get("mark_price")
            or info.get("markPx")
            or info.get("markPrice")
            or entry
        )
        contracts = float(pos.get("contracts") or 0)
        lev = int(pos.get("leverage") or info.get("leverage") or info.get("lever") or 0)
        margin = float(pos.get("margin_usdt") or 0)
        roe_pct, pnl_usd, notional, eff_lev = BlofinExchange.position_display_metrics(
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
        reg = registry.get(sym) or registry.get(key) or {}
        rows.append(
            {
                "position_key": key,
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
                "source": "bot",
            }
        )
    return rows


def publish_account_snapshot(
    state_dir: Path,
    equity: float,
    free_margin: float,
    positions: dict[str, dict],
    registry: PositionRegistry,
    *,
    api_ok: bool = True,
) -> None:
    """Write dashboard snapshot; never clobber good data with 429-empty reads."""
    state_dir.mkdir(parents=True, exist_ok=True)
    prev = _read_json(state_dir / ACCOUNT_SNAPSHOT)
    prev_positions = list(prev.get("positions") or [])
    prev_equity = float(prev.get("equity") or 0)
    prev_free = float(prev.get("free_margin") or 0)

    rows = build_position_rows(positions, registry) if positions else []
    if not api_ok:
        if not rows and prev_positions:
            rows = prev_positions
        if equity <= 0 and prev_equity > 0:
            equity = prev_equity
        if free_margin <= 0 and prev_free > 0:
            free_margin = prev_free

    unrealized = sum(float(r.get("pnl_usd") or 0) for r in rows)
    exposure = sum(float(r.get("notional_usdt") or 0) for r in rows)
    prof_path = state_dir / "profitability.json"
    prof_mtime = prof_path.stat().st_mtime if prof_path.is_file() else 0.0
    payload = {
        "updated_at": time.time(),
        "api_ok": api_ok,
        "equity": round(equity, 6),
        "free_margin": round(free_margin, 6),
        "unrealized_pnl": round(unrealized, 6),
        "exposure_usdt": round(exposure, 6),
        "open_count": len(rows),
        "positions": rows,
        "profitability_mtime": prof_mtime,
        "closed_version": prof_mtime,
    }
    _atomic_write(state_dir / ACCOUNT_SNAPSHOT, payload)
    # Back-compat for older hub paths
    _atomic_write(
        state_dir / LIVE_POSITIONS,
        {
            "updated_at": payload["updated_at"],
            "api_ok": api_ok,
            "count": len(rows),
            "positions": rows,
            "equity": payload["equity"],
            "free_margin": payload["free_margin"],
            "profitability_mtime": prof_mtime,
            "closed_version": prof_mtime,
        },
    )
    if equity > 0:
        from equity_ticks import append_equity_tick

        append_equity_tick(state_dir, equity, min_interval_sec=10.0, api_ok=api_ok)


def publish_live_book(
    state_dir: Path,
    positions: dict[str, dict],
    registry: PositionRegistry,
    *,
    closed_version: float | None = None,
    equity: float = 0.0,
    free_margin: float = 0.0,
    api_ok: bool = True,
) -> None:
    publish_account_snapshot(
        state_dir,
        equity,
        free_margin,
        positions,
        registry,
        api_ok=api_ok,
    )


def reconcile_account_snapshot(
    state_dir: Path,
    positions: dict[str, dict],
    registry: PositionRegistry,
    *,
    equity: float,
    free_margin: float,
    api_ok: bool = True,
) -> bool:
    """Auto-fix disk snapshot + registry when they drift from the live exchange book."""
    if not api_ok:
        return False
    open_syms = {
        str(p.get("symbol") or k.split("#")[0]) for k, p in positions.items()
    }
    registry.sync_with_exchange(open_syms, api_ok=True)
    prev = _read_json(state_dir / ACCOUNT_SNAPSHOT)
    prev_syms = {r.get("symbol") for r in prev.get("positions") or []}
    prev_count = int(prev.get("open_count") or -1)
    if prev_syms == open_syms and prev_count == len(open_syms):
        return False
    publish_account_snapshot(
        state_dir,
        equity,
        free_margin,
        positions,
        registry,
        api_ok=True,
    )
    return True


def load_account_snapshot(state_dir: Path, *, max_age_sec: float = 600.0) -> dict[str, Any] | None:
    path = state_dir / ACCOUNT_SNAPSHOT
    if not path.is_file():
        path = state_dir / LIVE_POSITIONS
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > max_age_sec:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Atomic JSON write with Windows-safe retry (bot + dashboard may race)."""
    data = json.dumps(payload, indent=2)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        for attempt in range(6):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt < 5:
                    time.sleep(0.04 * (attempt + 1))
                    continue
                path.write_text(data, encoding="utf-8")
                return
    except Exception:
        try:
            path.write_text(data, encoding="utf-8")
        except Exception:
            pass
    finally:
        if tmp.is_file():
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[arg-type]
            except TypeError:
                try:
                    tmp.unlink()
                except Exception:
                    pass
            except Exception:
                pass
