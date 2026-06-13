"""One-shot patch: seven winner-picking improvements."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_pick_engine() -> None:
    path = ROOT / "pick_engine.py"
    text = path.read_text(encoding="utf-8")

    anchor = 'ANCHOR_VOTES = frozenset({"htf_5m", "ml", "adx_trend", "structure", "ema_5m", "vwap"})'
    if "REGIME_MIN_PICK" not in text:
        text = text.replace(
            anchor,
            anchor
            + """

REGIME_MAX_CHASE = {
    "trending": 0.008,
    "climbing": 0.010,
    "ranging": 0.003,
    "choppy": 0.002,
    "mixed": 0.005,
    "volatile": 0.004,
}

REGIME_MIN_PICK = {
    "trending": 0.52,
    "climbing": 0.55,
    "ranging": 0.62,
    "choppy": 0.65,
    "mixed": 0.55,
    "volatile": 0.60,
}""",
        )

    gate_block = """    if opp_ratio > hard_opp:
        return PickVerdict(False, winner_score, f"opposition {opp_ratio:.0%} overwhelming")

    from runner_momentum import runner_priority_active"""

    new_gate_block = """    if opp_ratio > hard_opp:
        return PickVerdict(False, winner_score, f"opposition {opp_ratio:.0%} overwhelming")

    # Volatility gate — skip extreme ATR / wide spread
    atr_pct = getattr(cf, "atr_pct", 0.0)
    spread_pct = getattr(cf, "spread_pct", 0.0)
    max_atr = getattr(settings, "max_atr_pct", 0.025)
    max_spread = getattr(settings, "max_spread_pct", 0.0015)
    if atr_pct > max_atr:
        return PickVerdict(False, winner_score, f"vol gate ATR {atr_pct:.1%}")
    if spread_pct > max_spread:
        return PickVerdict(False, winner_score, f"vol gate spread {spread_pct:.3%}")

    # Pullback wait — reject chase unless elite/apex + ML tailwind
    from forward_pick import ml_direction_edge

    ml_edge = ml_direction_edge(ml_ctx, side)
    vd = cf.vwap_distance_pct
    chase = abs(vd)
    max_chase = REGIME_MAX_CHASE.get(cf.regime, 0.005)
    if chase > max_chase:
        bypass = winner_tier in ("elite", "apex") and ml_edge > 0.12
        if not bypass:
            return PickVerdict(
                False,
                winner_score,
                f"chase {chase:.2%} > {max_chase:.2%} — wait pullback",
            )

    # 15m HTF structure confirmation
    htf_15m = getattr(cf, "htf_15m_aligned", cf.htf_aligned)
    if not htf_15m and winner_tier not in ("elite", "apex"):
        if ml_edge < 0.08:
            return PickVerdict(
                False,
                winner_score,
                f"15m HTF misaligned — need ML edge (have {ml_edge:.2f})",
            )

    # Quality chop block
    quality_mode = False
    try:
        from quality_pick import quality_pick_active

        quality_mode = quality_pick_active(settings)
    except Exception:
        pass
    chop = getattr(cf, "chop_index", 0.5)
    path_eff = getattr(cf, "path_efficiency", 0.5)
    is_choppy_setup = getattr(cf, "is_choppy", False) or (
        chop >= 0.50 and path_eff < 0.30
    )
    if quality_mode and is_choppy_setup:
        if winner_tier not in ("elite", "apex"):
            return PickVerdict(
                False,
                winner_score,
                f"quality chop block chop={chop:.0%} path={path_eff:.0%}",
            )
        if ml_edge < 0.10:
            return PickVerdict(
                False,
                winner_score,
                f"quality chop — need ML edge (have {ml_edge:.2f})",
            )

    from runner_momentum import runner_priority_active"""

    if "vol gate ATR" not in text:
        text = text.replace(gate_block, new_gate_block)

    scoring_old = """    tier_boost = 0.06 if winner_tier == "elite" else 0.02
    pick = min(1.0, winner_score * 0.32 + fused * 0.68 + tier_boost)

    if sym_wr is not None and sym_wr >= 0.55:
        pick = min(1.0, pick + 0.04)

    min_pick = getattr(settings, "pick_min_score", 0.62)
    from quality_pick import quality_pick_active"""

    scoring_new = """    tier_boost = 0.08 if winner_tier == "apex" else (0.05 if winner_tier == "elite" else 0.02)
    pick = min(1.0, winner_score * 0.28 + fused * 0.72 + tier_boost)

    from forward_pick import forward_pick_adjustments

    fwd_boost, floor_cut = forward_pick_adjustments(
        state_dir=settings.state_dir,
        symbol=symbol,
        side=side,
        ml_ctx=ml_ctx,
        fast_win=fast,
        winner_tier=winner_tier,
    )
    pick = min(1.0, pick + fwd_boost)

    if sym_wr is not None and sym_wr >= 0.55:
        pick = min(1.0, pick + 0.04)

    min_pick = max(
        getattr(settings, "pick_min_score", 0.62),
        REGIME_MIN_PICK.get(cf.regime, 0.55),
    )
    from quality_pick import quality_pick_active"""

    if "forward_pick_adjustments" not in text:
        text = text.replace(scoring_old, scoring_new)

    never_loosen_old = """    never_loosen = quality_mode or getattr(settings, "entries_never_pause", False)
    if never_loosen:
        wr, _pf = (0.5, 1.0)
        try:
            from quality_pick import live_performance

            wr, _pf = live_performance(settings)
        except Exception:
            pass
        if wr < 0.42:
            min_pick = max(min_pick, 0.58)
        elif wr < 0.48:
            min_pick = max(min_pick, 0.55)
        if winner_tier not in ("elite", "apex"):
            min_pick = max(min_pick, 0.52)
    elif starved:
        min_pick = min(min_pick, 0.42 if hourly_3r_active(settings) else 0.48)
    if pick < min_pick:"""

    never_loosen_new = """    never_loosen = quality_mode or getattr(settings, "entries_never_pause", False)
    ml_forward_strong = fwd_boost >= 0.06 or floor_cut >= 0.04
    wr, _pf = (0.5, 1.0)
    if never_loosen:
        try:
            from quality_pick import live_performance

            wr, _pf = live_performance(settings)
        except Exception:
            pass
        if wr < 0.42:
            min_pick = max(min_pick, 0.58)
        elif wr < 0.48:
            min_pick = max(min_pick, 0.55)
        if winner_tier not in ("elite", "apex"):
            if not (starved and ml_forward_strong):
                min_pick = max(min_pick, 0.52)
        if starved and ml_forward_strong:
            starved_cap = 0.46 if hourly_3r_active(settings) else 0.50
            min_pick = min(min_pick, starved_cap)
    elif starved:
        min_pick = min(min_pick, 0.42 if hourly_3r_active(settings) else 0.48)
    min_pick = max(0.48, min_pick - floor_cut)

    if never_loosen and ml_ctx.ready and ml_edge < -0.06:
        return PickVerdict(
            False,
            pick,
            f"ML headwind edge={ml_edge:.2f}",
            fast_win=fast,
        )
    if never_loosen and wr < 0.45:
        if not ml_ctx.ready or ml_edge < 0.05:
            return PickVerdict(
                False,
                pick,
                f"weak live WR — need ML tailwind (edge={ml_edge:.2f})",
                fast_win=fast,
            )
        if fast < 0.48:
            return PickVerdict(
                False,
                pick,
                f"weak live WR — fast_win {fast:.2f} < 0.48",
                fast_win=fast,
            )

    if pick < min_pick:"""

    if "ML headwind" not in text:
        text = text.replace(never_loosen_old, never_loosen_new)

    reason_old = """    reason = f"fused={fused:.2f} fast={fast:.2f} trend={trend:.2f} " + "+".join((tags + trend_tags)[:4])
    log.info("""

    reason_new = """    reason = f"fused={fused:.2f} fast={fast:.2f} trend={trend:.2f} " + "+".join((tags + trend_tags)[:4])
    if fwd_boost > 0 or floor_cut > 0:
        reason += f" fwd=+{fwd_boost:.2f} floor_cut={floor_cut:.2f}"
    log.info("""

    if "fwd=+" not in text:
        text = text.replace(reason_old, reason_new)

    short_check = """    if ml_ctx.ready and side == Signal.LONG and ml_ctx.long_precision < 0.42:
        if ml_ctx.p_long < ml_ctx.p_short + 0.14:
            return PickVerdict(
                False,
                pick,
                f"long OOS weak p={ml_ctx.long_precision:.0%} ml edge insufficient",
                fast_win=fast,
            )

    reason = """

    short_check_new = """    if ml_ctx.ready and side == Signal.LONG and ml_ctx.long_precision < 0.42:
        if ml_ctx.p_long < ml_ctx.p_short + 0.14:
            return PickVerdict(
                False,
                pick,
                f"long OOS weak p={ml_ctx.long_precision:.0%} ml edge insufficient",
                fast_win=fast,
            )
    if ml_ctx.ready and side == Signal.SHORT and ml_ctx.short_precision < 0.42:
        if ml_ctx.p_short < ml_ctx.p_long + 0.14:
            return PickVerdict(
                False,
                pick,
                f"short OOS weak p={ml_ctx.short_precision:.0%} ml edge insufficient",
                fast_win=fast,
            )

    reason = """

    if "short OOS weak" not in text:
        text = text.replace(short_check, short_check_new)

    path.write_text(text, encoding="utf-8")
    print("patched pick_engine.py")


def patch_ta_confluence() -> None:
    path = ROOT / "ta_confluence.py"
    text = path.read_text(encoding="utf-8")

    if "atr_pct:" not in text:
        text = text.replace(
            "    is_choppy: bool = False\n    votes: list[TAVote]",
            "    is_choppy: bool = False\n    atr_pct: float = 0.0\n    spread_pct: float = 0.0\n    htf_15m_aligned: bool = False\n    votes: list[TAVote]",
        )

    if "def _htf_15m_vote" not in text:
        insert_after = 'def _htf_vote(closes_5m: list[float]) -> TAVote:'
        idx = text.find(insert_after)
        if idx >= 0:
            end = text.find("\n\n", idx)
            htf_15m_fn = '''

def _htf_15m_vote(closes_15m: list[float]) -> TAVote:
    """15m structure vote — slower anchor for entry confirmation."""
    if len(closes_15m) < 25:
        return _vote("htf_15m", Signal.FLAT, 0.0, 1.4, "no 15m data")
    bias = _htf_bias(closes_15m)
    if bias == "long":
        return _vote("htf_15m", Signal.LONG, 0.82, 1.4, "15m uptrend")
    if bias == "short":
        return _vote("htf_15m", Signal.SHORT, 0.82, 1.4, "15m downtrend")
    return _vote("htf_15m", Signal.FLAT, 0.0, 1.4, "15m flat")
'''
            text = text[:end] + htf_15m_fn + text[end:]

    sig_old = """def run_all_analyses(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]],
    *,
    funding_rate: float | None = None,"""

    sig_new = """def run_all_analyses(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]],
    *,
    ohlcv_15m: list[list[float]] | None = None,
    funding_rate: float | None = None,"""

    if "ohlcv_15m:" not in text:
        text = text.replace(sig_old, sig_new)

    votes_old = """        _htf_vote(closes_5m),
        _adx_vote(ohlcv_1m, closes),"""

    votes_new = """        _htf_vote(closes_5m),
        _htf_15m_vote([row[4] for row in ohlcv_15m] if ohlcv_15m else []),
        _adx_vote(ohlcv_1m, closes),"""

    if '_htf_15m_vote' in text and "_htf_15m_vote(" not in text.split("votes: list[TAVote]")[0]:
        text = text.replace(votes_old, votes_new)

    htf_block = """    htf = _htf_bias(closes_5m)
    htf_aligned = (direction == Signal.LONG and htf == "long") or (direction == Signal.SHORT and htf == "short")

    atr_v = atr(ohlcv_1m, 14)"""

    htf_block_new = """    htf = _htf_bias(closes_5m)
    htf_aligned = (direction == Signal.LONG and htf == "long") or (direction == Signal.SHORT and htf == "short")
    closes_15m = [row[4] for row in ohlcv_15m] if ohlcv_15m else []
    htf_15m = _htf_bias(closes_15m) if len(closes_15m) >= 25 else htf
    htf_15m_aligned = (direction == Signal.LONG and htf_15m == "long") or (
        direction == Signal.SHORT and htf_15m == "short"
    )

    atr_v = atr(ohlcv_1m, 14)"""

    if "htf_15m_aligned" not in text.split("return ConfluenceResult")[0]:
        text = text.replace(htf_block, htf_block_new)

    result_old = """        is_runner=is_runner,
        is_choppy=is_choppy,
        votes=votes,
    )"""

    result_new = """        is_runner=is_runner,
        is_choppy=is_choppy,
        atr_pct=round(atr_pct_val, 5) if atr_pct_val else 0.0,
        spread_pct=round(spread_pct_val, 5) if spread_pct_val else 0.0,
        htf_15m_aligned=htf_15m_aligned,
        votes=votes,
    )"""

    if "atr_pct_val" not in text:
        text = text.replace(
            "    if atr_v is None or close <= 0:\n        stop_pct, take_pct =",
            "    atr_pct_val = (atr_v / close) if atr_v and close > 0 else 0.0\n    last = ohlcv_1m[-1]\n    hi = float(last[2]) if len(last) > 2 else close\n    lo = float(last[3]) if len(last) > 3 else close\n    spread_pct_val = ((hi - lo) / close) if close > 0 else 0.0\n\n    if atr_v is None or close <= 0:\n        stop_pct, take_pct =",
        )
        text = text.replace(result_old, result_new)

    path.write_text(text, encoding="utf-8")
    print("patched ta_confluence.py")


def patch_signals() -> None:
    path = ROOT / "signals.py"
    text = path.read_text(encoding="utf-8")

    fetch_old = """    ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", 100)
    ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", 50)"""

    fetch_new = """    ohlcv_1m = ex.fetch_ohlcv(symbol, "1m", 100)
    ohlcv_5m = ex.fetch_ohlcv(symbol, "5m", 50)
    ohlcv_15m = ex.fetch_ohlcv(symbol, "15m", 40)"""

    if "ohlcv_15m = ex.fetch_ohlcv" not in text:
        text = text.replace(fetch_old, fetch_new)

    call_old = """    cf = run_all_analyses(
        ohlcv_1m,
        ohlcv_5m,"""

    call_new = """    cf = run_all_analyses(
        ohlcv_1m,
        ohlcv_5m,
        ohlcv_15m=ohlcv_15m,"""

    if "ohlcv_15m=ohlcv_15m" not in text:
        text = text.replace(call_old, call_new)

    path.write_text(text, encoding="utf-8")
    print("patched signals.py")


def patch_forward_pick() -> None:
    path = ROOT / "forward_pick.py"
    text = path.read_text(encoding="utf-8")

    if "def symbol_forward_blocked" not in text:
        text += '''

def symbol_forward_blocked(
    state_dir: Path,
    symbol: str,
    side: str,
    *,
    cold_wr: float = 0.30,
    min_trades: int = 4,
) -> tuple[bool, str]:
    """Block symbol/side with poor forward win-rate feedback."""
    sym_wr, n = symbol_forward_wr(state_dir, symbol, side, min_trades=min_trades)
    if sym_wr is not None and n >= min_trades and sym_wr < cold_wr:
        return True, f"forward wr {sym_wr:.0%} n={n}"
    return False, ""


def symbol_forward_boost(
    state_dir: Path,
    symbol: str,
    side: str,
) -> float:
    """Auto-boost proven forward winners."""
    sym_wr, n = symbol_forward_wr(state_dir, symbol, side, min_trades=3)
    if sym_wr is None:
        return 0.0
    if sym_wr >= 0.65 and n >= 4:
        return 0.06
    if sym_wr >= 0.55 and n >= 3:
        return 0.03
    return 0.0
'''
        # Also wire boost into forward_pick_adjustments
        text = text.replace(
            "    if sym_wr is not None:\n        if sym_wr >= 0.55:",
            "    sym_wr, _sym_n = symbol_forward_wr(state_dir, symbol, side.value if side != Signal.FLAT else \"\")\n    boost += symbol_forward_boost(state_dir, symbol, side.value if side != Signal.FLAT else \"\")\n\n    if sym_wr is not None:\n        if sym_wr >= 0.55:",
        )
        # Fix duplicate sym_wr line - the original already has sym_wr fetch
        text = text.replace(
            "    sym_wr, _sym_n = symbol_forward_wr(state_dir, symbol, side.value if side != Signal.FLAT else \"\")\n    boost += symbol_forward_boost(state_dir, symbol, side.value if side != Signal.FLAT else \"\")\n\n    sym_wr, _sym_n = symbol_forward_wr(state_dir, symbol, side.value if side != Signal.FLAT else \"\")",
            "    sym_wr, _sym_n = symbol_forward_wr(state_dir, symbol, side.value if side != Signal.FLAT else \"\")\n    boost += symbol_forward_boost(state_dir, symbol, side.value if side != Signal.FLAT else \"\")",
        )

    path.write_text(text, encoding="utf-8")
    print("patched forward_pick.py")


def patch_quality_pick() -> None:
    path = ROOT / "quality_pick.py"
    text = path.read_text(encoding="utf-8")

    if "def choppy_side_blocked" not in text:
        choppy_fn = '''

def choppy_side_blocked(settings: "Settings", symbol: str, side: str) -> tuple[bool, str]:
    """Block symbol/side after repeated choppy-entry losses."""
    if not quality_pick_active(settings):
        return False, ""
    try:
        from roe_learning import get_roe_store

        store = get_roe_store(settings.state_dir)
        side_key = str(side).lower()
        recent = [
            r
            for r in (store._data.get("global", {}).get("recent") or [])
            if str(r.get("symbol") or "") == symbol
            and str(r.get("side") or "").lower() == side_key
        ][-4:]
    except Exception:
        return False, ""
    if len(recent) < 2:
        return False, ""
    chop_losses = sum(
        1
        for r in recent
        if float(r.get("roe_pct") or 0) < 0 and abs(float(r.get("roe_pct") or 0)) >= 12.0
    )
    if chop_losses >= 2:
        return True, f"{side_key} on {symbol.split('/')[0]} repeated chop losses"
    return False, ""
'''
        text = text.replace(
            "def entry_blocked_by_live_roe(",
            choppy_fn + "\ndef entry_blocked_by_live_roe(",
        )

    if "choppy_side_blocked" not in text.split("entry_blocked_by_live_roe")[1]:
        text = text.replace(
            """    blocked, reason = symbol_entry_blocked(settings, symbol)
    if blocked:
        return True, reason

    side_key = str(side).lower()""",
            """    blocked, reason = symbol_entry_blocked(settings, symbol)
    if blocked:
        return True, reason

    chop_blocked, chop_reason = choppy_side_blocked(settings, symbol, side)
    if chop_blocked:
        return True, chop_reason

    try:
        from forward_pick import symbol_forward_blocked

        fwd_blocked, fwd_reason = symbol_forward_blocked(
            settings.state_dir, symbol, str(side).lower()
        )
        if fwd_blocked:
            return True, fwd_reason
    except Exception:
        pass

    side_key = str(side).lower()""",
        )

    path.write_text(text, encoding="utf-8")
    print("patched quality_pick.py")


def patch_ml_calibration() -> None:
    cal_path = ROOT / "ml" / "calibration.py"
    if not cal_path.exists():
        cal_path.write_text(
            '''"""Platt-style probability calibration for ML outputs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def load_calibrator(state_dir: Path) -> dict | None:
    path = state_dir / "ml_calibration.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_calibrator(state_dir: Path, long_a: float, long_b: float, short_a: float, short_b: float) -> None:
    path = state_dir / "ml_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "long": {"a": long_a, "b": long_b},
                "short": {"a": short_a, "b": short_b},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def fit_platt(raw_probs: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """Simple Platt scaling via logit regression (2-param)."""
    eps = 1e-6
    p = np.clip(raw_probs, eps, 1 - eps)
    logit = np.log(p / (1 - p))
    X = np.column_stack([logit, np.ones(len(logit))])
    try:
        coef, _, _, _ = np.linalg.lstsq(X, labels.astype(float), rcond=None)
        return float(coef[0]), float(coef[1])
    except Exception:
        return 1.0, 0.0


def calibrate_side(p_raw: float, side: str, cal: dict | None) -> float:
    if not cal:
        return p_raw
    row = cal.get("long" if side == "long" else "short") or {}
    a = float(row.get("a", 1.0))
    b = float(row.get("b", 0.0))
    eps = 1e-6
    p = max(eps, min(1 - eps, p_raw))
    logit = np.log(p / (1 - p))
    return float(_sigmoid(a * logit + b))


def calibrate_pair(p_long: float, p_short: float, state_dir: Path) -> tuple[float, float]:
    cal = load_calibrator(state_dir)
    if not cal:
        return p_long, p_short
    cl = calibrate_side(p_long, "long", cal)
    cs = calibrate_side(p_short, "short", cal)
    total = cl + cs
    if total <= 0:
        return p_long, p_short
    return cl / total, cs / total
''',
            encoding="utf-8",
        )
        print("created ml/calibration.py")

    trainer = ROOT / "ml" / "universe_trainer.py"
    text = trainer.read_text(encoding="utf-8")
    if "ml.calibration" not in text and "def _maybe_fit_calibration" not in text:
        hook = """        self._last_refit_ts = time.time()
        self._save_state()
        if self.on_model_updated:
            self.on_model_updated()"""
        new_hook = """        self._last_refit_ts = time.time()
        self._maybe_fit_calibration()
        self._save_state()
        if self.on_model_updated:
            self.on_model_updated()"""
        if hook in text:
            text = text.replace(hook, new_hook)

        cal_method = '''

    def _maybe_fit_calibration(self) -> None:
        """Fit Platt calibrators from recent outcome labels."""
        try:
            import numpy as np
            from ml.calibration import fit_platt, save_calibrator
            from ml.outcomes import TradeOutcomeTracker

            tracker = TradeOutcomeTracker(self.settings.state_dir)
            rows = tracker.recent_labeled(limit=400)
            if len(rows) < 40:
                return
            long_p = np.array([float(r.get("p_long") or 0.5) for r in rows])
            short_p = np.array([float(r.get("p_short") or 0.5) for r in rows])
            y_long = np.array([1.0 if r.get("side") == "long" and r.get("win") else 0.0 for r in rows])
            y_short = np.array([1.0 if r.get("side") == "short" and r.get("win") else 0.0 for r in rows])
            if y_long.sum() >= 8:
                la, lb = fit_platt(long_p, y_long)
            else:
                la, lb = 1.0, 0.0
            if y_short.sum() >= 8:
                sa, sb = fit_platt(short_p, y_short)
            else:
                sa, sb = 1.0, 0.0
            save_calibrator(self.settings.state_dir, la, lb, sa, sb)
        except Exception as exc:
            log.debug("calibration skip: %s", exc)
'''
        text = text.replace("\nlog = logging.getLogger(__name__)", "\nlog = logging.getLogger(__name__)" + cal_method)
        trainer.write_text(text, encoding="utf-8")
        print("patched ml/universe_trainer.py")

    predictor = ROOT / "ml" / "predictor.py"
    ptext = predictor.read_text(encoding="utf-8")
    if "calibrate_pair" not in ptext:
        ptext = ptext.replace(
            "        return p_long, p_short",
            "        try:\n            from ml.calibration import calibrate_pair\n\n            p_long, p_short = calibrate_pair(p_long, p_short, self.state_dir)\n        except Exception:\n            pass\n        return p_long, p_short",
        )
        predictor.write_text(ptext, encoding="utf-8")
        print("patched ml/predictor.py")


def patch_bot_probe_entry() -> None:
    path = ROOT / "bot.py"
    text = path.read_text(encoding="utf-8")

    probe_old = """    result = ex.open_position(
        symbol=symbol,
        side=decision.signal.value,
        contracts=plan.contracts,"""

    probe_new = """    entry_contracts = plan.contracts
    try:
        from quality_pick import quality_pick_active

        tier = str(getattr(decision, "winner_tier", "") or "")
        if quality_pick_active(settings) and tier not in ("elite", "apex"):
            probe_frac = float(getattr(settings, "entry_probe_fraction", 0.55))
            min_sz = float(getattr(plan, "min_contract_size", 0) or 0)
            if probe_frac > 0 and probe_frac < 1.0 and entry_contracts > 0:
                import math

                probed = entry_contracts * probe_frac
                if min_sz > 0:
                    probed = max(min_sz, math.floor(probed / min_sz) * min_sz)
                if probed >= min_sz and probed < entry_contracts:
                    entry_contracts = probed
                    log.info(
                        "PROBE entry %s %s size=%.4f (%.0f%% of plan)",
                        symbol.split("/")[0],
                        decision.signal.value,
                        entry_contracts,
                        probe_frac * 100,
                    )
    except Exception:
        entry_contracts = plan.contracts

    result = ex.open_position(
        symbol=symbol,
        side=decision.signal.value,
        contracts=entry_contracts,"""

    if "PROBE entry" not in text:
        text = text.replace(probe_old, probe_new)
        path.write_text(text, encoding="utf-8")
        print("patched bot.py")


def main() -> None:
    patch_pick_engine()
    patch_ta_confluence()
    patch_signals()
    patch_forward_pick()
    patch_quality_pick()
    patch_ml_calibration()
    patch_bot_probe_entry()
    print("all patches applied")


if __name__ == "__main__":
    main()
