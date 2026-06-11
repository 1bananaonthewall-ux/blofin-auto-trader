"""
Backtest-aligned confluence entry path — evaluate_entry + portfolio rank (no winner/pick/swarm).

Used by God Bot supercharge mode so live logic matches growth portfolio backtest.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from bobs_bots.evaluator import evaluate_entry
from bobs_bots.regime import rolling_period_bias
from bobs_bots.specs import BotSpec
from conviction import RankedSetup

if TYPE_CHECKING:
    from blofin_http import BlofinExchange
    from config import Settings

log = logging.getLogger(__name__)


def spec_from_policy(pol: dict[str, Any]) -> BotSpec:
    """BotSpec tuned from growth_supercharge.json (matches growth_agent_bt)."""
    return BotSpec(
        id="god-bot-confluence",
        name="God Bot Confluence Core",
        description="Backtest-aligned 3R confluence entries",
        min_confluence=float(pol.get("min_confluence", 0.47)),
        min_agreeing=int(pol.get("min_agreeing", 4)),
        min_composite_score=float(pol.get("min_signal_score", 47.0)),
        min_confidence=float(pol.get("min_confidence", 0.55)),
        runner_filter=True,
        require_runner=False,
        skip_choppy=bool(pol.get("skip_choppy", True)),
        min_runner_score=0.38,
        max_chop=0.58,
        min_path_eff=0.18,
        three_r_mode=True,
        max_stop_pct=float(pol.get("sl_pct", 1.0)) / 100.0,
        max_take_pct=float(pol.get("tp_pct", 3.0)) / 100.0,
        min_rr=3.0,
        atr_stop_mult=1.10,
        atr_take_mult=2.0,
        risk_per_trade=float(pol.get("margin_pct_per_trade", 2.2)) / 100.0,
        entry_gap_bars=int(pol.get("entry_gap_bars", 5)),
        min_adx_1h=8.0,
        pullback_band=0.004,
    )


def confluence_conviction(dec: Any) -> float:
    conf = float(getattr(dec, "model_confidence", 0.0) or (dec.score / 100.0))
    conv = conf * (dec.score / 100.0)
    if getattr(dec.signal, "value", "") == "short":
        conv *= 1.02
    return conv


def _volume_rank_symbols(ex: "BlofinExchange", symbols: list[str], top_n: int) -> list[str]:
    try:
        tickers = {t["instId"]: t for t in ex.http.list_tickers()}
    except Exception:
        return symbols[:top_n]
    from markets import symbol_to_inst_id

    ranked: list[tuple[float, str]] = []
    for sym in symbols:
        inst = symbol_to_inst_id(sym)
        vol = float((tickers.get(inst) or {}).get("volCurrency24h") or 0)
        if vol > 50_000:
            ranked.append((vol, sym))
    ranked.sort(reverse=True)
    return [s for _, s in ranked[:top_n]]


def scan_symbol(
    ex: "BlofinExchange",
    sym: str,
    spec: BotSpec,
    *,
    min_conf: float,
    min_score: float,
    start_ms: int,
) -> Any | None:
    ohlcv_1m = ex.fetch_ohlcv(sym, "1m", 100)
    ohlcv_5m = ex.fetch_ohlcv(sym, "5m", 50)
    if len(ohlcv_1m) < 40 or len(ohlcv_5m) < 30:
        return None
    funding = ex.fetch_funding_rate(sym)
    ts = ohlcv_5m[-1][0]
    bias = rolling_period_bias(ohlcv_5m, ts, start_ms=start_ms)
    dec = evaluate_entry(
        ohlcv_1m,
        ohlcv_5m,
        spec,
        funding_rate=funding,
        period_bias=bias,
        ohlcv_1h=ohlcv_5m,
    )
    if dec is None:
        return None
    conf = dec.model_confidence or (dec.score / 100.0)
    if conf < min_conf or dec.score < min_score:
        return None
    return dec


def scan_and_rank(
    ex: "BlofinExchange",
    settings: "Settings",
    symbols: list[str],
    held: set[str],
    cooldowns,
    knobs,
    *,
    equity: float,
) -> tuple[list[RankedSetup], int]:
    from growth_supercharge import get_brain, load_policy, rank_boost, symbol_blocked

    pol = load_policy(settings.state_dir)
    brain = get_brain(settings)
    spec = spec_from_policy(pol)
    min_conf = float(pol["min_confidence"])
    min_score = float(pol["min_signal_score"])
    if brain is not None:
        b_conf, b_score, _ = brain.effective_gates()
        min_conf = max(min_conf, b_conf)
        min_score = max(min_score, b_score)

    top_n = int(pol.get("scan_top_n", 60))
    scan_syms = _volume_rank_symbols(ex, [s for s in symbols if s not in held], top_n)
    start_ms = int(time.time() * 1000) - 7 * 86400 * 1000

    stream = getattr(ex, "stream", None)
    if stream is not None and scan_syms:
        from markets import symbol_to_inst_id

        pri = [symbol_to_inst_id(s) for s in scan_syms[: min(60, len(scan_syms))]]
        stream.set_priority(pri)
        boot_n = min(40, len(scan_syms))
        for sym in scan_syms[:boot_n]:
            stream.bootstrap_candles(sym, "1m", 100)
            stream.bootstrap_candles(sym, "5m", 50)

    pool: list[RankedSetup] = []
    for sym in scan_syms:
        if cooldowns.is_blocked(sym):
            continue
        blocked, reason = symbol_blocked(brain, sym)
        if blocked:
            log.debug("confluence skip %s: %s", sym.split("/")[0], reason)
            continue
        try:
            dec = scan_symbol(ex, sym, spec, min_conf=min_conf, min_score=min_score, start_ms=start_ms)
            if dec is None:
                continue
            label = str(getattr(dec, "run_label", "") or "")
            choppy = bool(getattr(dec, "is_choppy", False))
            blocked, reason = symbol_blocked(brain, sym, run_label=label, is_choppy=choppy)
            if blocked:
                continue
            conf = dec.model_confidence or (dec.score / 100.0)
            conv = confluence_conviction(dec)
            side = dec.signal.value
            conv = rank_boost(brain, sym, side, conv)
            pool.append(
                RankedSetup(symbol=sym, decision=dec, conviction=conv, confidence=conf, score=dec.score)
            )
        except Exception:
            log.debug("confluence scan fail %s", sym.split("/")[0], exc_info=True)
        time.sleep(0.04)

    pool.sort(key=lambda r: r.conviction, reverse=True)
    if pool:
        top = pool[0]
        log.info(
            "confluence scan: %d/%d pass | top %s %s conv=%.3f conf=%.2f score=%.0f",
            len(pool),
            len(scan_syms),
            top.symbol.split("/")[0],
            top.decision.signal.value,
            top.conviction,
            top.confidence,
            top.score,
        )
    return pool, len(scan_syms)


def select_top_opens(
    ranked: list[RankedSetup],
    *,
    max_opens: int,
    policy: dict[str, Any],
    knobs,
) -> list[RankedSetup]:
    """Open only the best-ranked setups — no tie loosening or apex tiers."""
    if not ranked:
        return []
    cap = int(policy.get("max_opens_per_cycle", 0) or 0)
    n = max_opens if cap <= 0 else max(1, min(max_opens, cap))
    floor = float(policy["min_confidence"]) * (float(policy["min_signal_score"]) / 100.0) * 0.82
    floor = max(floor, knobs.min_confidence * (knobs.min_signal_score / 100.0) * 0.80)
    elite = [r for r in ranked[:n] if r.conviction >= floor]
    return elite
