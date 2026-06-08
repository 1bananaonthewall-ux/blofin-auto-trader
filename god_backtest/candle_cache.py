"""Parallel OHLCV history + disk cache. WS used for live tail sync only."""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from storefront_market import fetch_candles_history

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "state" / "god_backtest" / "candles"
_LOAD_LOCK = threading.Lock()


def _cache_key(inst_id: str, bar: str, start_ms: int, end_ms: int) -> str:
    safe = inst_id.replace("/", "_")
    return f"{safe}_{bar}_{start_ms}_{end_ms}"


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json.gz"


def load_bars(
    inst_id: str,
    bar: str,
    *,
    start_ms: int,
    end_ms: int,
    use_cache: bool = True,
) -> list[list[float]]:
    key = _cache_key(inst_id, bar, start_ms, end_ms)
    path = _cache_path(key)
    if use_cache and path.is_file():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                return data
        except Exception:
            path.unlink(missing_ok=True)

    bars = fetch_candles_history(inst_id, bar=bar, start_ms=start_ms, end_ms=end_ms)
    if bars and use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                json.dump(bars, fh, separators=(",", ":"))
        except Exception as exc:
            log.debug("cache write %s failed: %s", inst_id, exc)
    return bars


def load_symbol_candles(
    inst_id: str,
    *,
    start_ms: int,
    end_ms: int,
    warmup_5m: int = 120,
    lookback_1h: int = 60,
    use_cache: bool = True,
) -> tuple[list[list[float]], list[list[float]]]:
    candles_5m = load_bars(
        inst_id,
        "5m",
        start_ms=start_ms - warmup_5m * 300_000,
        end_ms=end_ms,
        use_cache=use_cache,
    )
    candles_1h = load_bars(
        inst_id,
        "1H",
        start_ms=start_ms - lookback_1h * 3_600_000,
        end_ms=end_ms,
        use_cache=use_cache,
    )
    return candles_5m, candles_1h


def prefetch_universe(
    inst_ids: list[str],
    *,
    start_ms: int,
    end_ms: int,
    max_workers: int = 8,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Parallel REST history fetch (fast path for full-universe backtests)."""
    ok = 0
    errors = 0
    bars_5m: dict[str, int] = {}
    t0 = time.time()

    def _one(iid: str) -> tuple[str, int, int]:
        c5, c1 = load_symbol_candles(iid, start_ms=start_ms, end_ms=end_ms, use_cache=use_cache)
        return iid, len(c5), len(c1)

    workers = max(1, min(max_workers, len(inst_ids) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, iid): iid for iid in inst_ids}
        for fut in as_completed(futures):
            iid = futures[fut]
            try:
                _, n5, n1 = fut.result()
                if n5 >= 140 and n1 >= 30:
                    ok += 1
                    bars_5m[iid] = n5
                else:
                    errors += 1
            except Exception as exc:
                errors += 1
                log.debug("prefetch %s failed: %s", iid, exc)

    return {
        "symbols_ok": ok,
        "symbols_errors": errors,
        "elapsed_sec": round(time.time() - t0, 1),
        "cache_dir": str(CACHE_DIR),
    }


def merge_ws_tail(
    inst_id: str,
    bar: str,
    bars: list[list[float]],
    tail: list[list[float]],
) -> list[list[float]]:
    """Merge websocket tail candles onto REST history (dedupe by ts)."""
    if not tail:
        return bars
    by_ts = {int(b[0]): b for b in bars}
    for row in tail:
        if len(row) >= 5:
            by_ts[int(row[0])] = row
    return [by_ts[k] for k in sorted(by_ts.keys())]
