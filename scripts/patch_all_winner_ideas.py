"""Apply all winner-picking idea integrations."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_blofin_http() -> None:
    path = ROOT / "blofin_http.py"
    text = path.read_text(encoding="utf-8")
    if "def extract_order_id" not in text:
        text = text.replace(
            'log = logging.getLogger(__name__)\n\nBASE_URL',
            '''log = logging.getLogger(__name__)


def normalize_order_row(result):
    if isinstance(result, list) and result:
        row = result[0]
        return row if isinstance(row, dict) else {"orderId": row}
    if isinstance(result, dict):
        return result
    return {"orderId": result}


def extract_order_id(result) -> str:
    row = normalize_order_row(result)
    oid = row.get("orderId") or row.get("ordId") or row.get("id")
    return str(oid or "").strip()


BASE_URL''',
        )
    if "body=[{" not in text and "def cancel_order" in text:
        text = text.replace(
            '''    def cancel_order(self, inst_id: str, order_id: str) -> Any:
        """Cancel a resting (unfilled / partially filled) order by id."""
        return self.request(
            "POST",
            "/api/v1/trade/cancel-order",
            body={"instId": inst_id, "orderId": str(order_id)},
        )''',
            '''    def cancel_order(self, inst_id: str, order_id: str) -> Any:
        """Cancel a resting (unfilled / partially filled) order by id."""
        oid = str(order_id or "").strip()
        if not oid or oid.startswith("["):
            raise ValueError(f"invalid orderId for cancel: {order_id!r}")
        return self.request(
            "POST",
            "/api/v1/trade/cancel-order",
            body=[{"instId": inst_id, "orderId": oid, "clientOrderId": ""}],
        )''',
        )
    if "normalize_order_row(result)" not in text.split("def place_order")[1][:400]:
        text = text.replace(
            "        return result if isinstance(result, dict) else {\"orderId\": result}",
            "        return normalize_order_row(result)",
        )
    path.write_text(text, encoding="utf-8")
    print("patched blofin_http.py")


def patch_market_stream() -> None:
    path = ROOT / "market_stream.py"
    text = path.read_text(encoding="utf-8")
    if "def get_spread_pct" not in text:
        text = text.replace(
            "    def get_ticker(self, symbol: str) -> dict[str, Any] | None:",
            '''    def get_spread_pct(self, symbol: str) -> float:
        row = self.get_ticker(symbol)
        if not row:
            return 0.0
        try:
            bid = float(row.get("bidPrice") or row.get("bid") or 0)
            ask = float(row.get("askPrice") or row.get("ask") or 0)
            if bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
                return (ask - bid) / mid if mid > 0 else 0.0
        except (TypeError, ValueError):
            pass
        return 0.0

    def get_ticker(self, symbol: str) -> dict[str, Any] | None:''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched market_stream.py")


def patch_ta_confluence() -> None:
    path = ROOT / "ta_confluence.py"
    text = path.read_text(encoding="utf-8")
    if "htf_1h_aligned" not in text:
        text = text.replace(
            "    htf_15m_aligned: bool = False\n    votes: list[TAVote]",
            "    htf_15m_aligned: bool = False\n    htf_1h_aligned: bool = False\n    book_spread_pct: float = 0.0\n    votes: list[TAVote]",
        )
    if "def _htf_1h_vote" not in text:
        text = text.replace(
            'return _vote("htf_15m", Signal.FLAT, 0.0, 1.4, "15m flat")\n\n\n\ndef _adx_vote',
            '''return _vote("htf_15m", Signal.FLAT, 0.0, 1.4, "15m flat")


def _htf_1h_vote(closes_1h: list[float]) -> TAVote:
    if len(closes_1h) < 25:
        return _vote("htf_1h", Signal.FLAT, 0.0, 1.5, "no 1h data")
    bias = _htf_bias(closes_1h)
    if bias == "long":
        return _vote("htf_1h", Signal.LONG, 0.85, 1.5, "1h uptrend")
    if bias == "short":
        return _vote("htf_1h", Signal.SHORT, 0.85, 1.5, "1h downtrend")
    return _vote("htf_1h", Signal.FLAT, 0.0, 1.5, "1h flat")


def _adx_vote''',
        )
    if "ohlcv_1h:" not in text:
        text = text.replace(
            "    ohlcv_15m: list[list[float]] | None = None,\n    funding_rate:",
            "    ohlcv_15m: list[list[float]] | None = None,\n    ohlcv_1h: list[list[float]] | None = None,\n    book_spread_pct: float = 0.0,\n    funding_rate:",
        )
    if '_htf_1h_vote' not in text.split("votes: list[TAVote]")[0]:
        text = text.replace(
            "        _htf_15m_vote([row[4] for row in ohlcv_15m] if ohlcv_15m else []),\n        _adx_vote",
            "        _htf_15m_vote([row[4] for row in ohlcv_15m] if ohlcv_15m else []),\n        _htf_1h_vote([row[4] for row in ohlcv_1h] if ohlcv_1h else []),\n        _adx_vote",
        )
    if "htf_1h_aligned" not in text.split("return ConfluenceResult")[0]:
        text = text.replace(
            "    htf_15m_aligned = (direction == Signal.LONG and htf_15m == \"long\") or (\n        direction == Signal.SHORT and htf_15m == \"short\"\n    )\n\n    atr_v",
            "    htf_15m_aligned = (direction == Signal.LONG and htf_15m == \"long\") or (\n        direction == Signal.SHORT and htf_15m == \"short\"\n    )\n    closes_1h = [row[4] for row in ohlcv_1h] if ohlcv_1h else []\n    htf_1h = _htf_bias(closes_1h) if len(closes_1h) >= 25 else htf_15m\n    htf_1h_aligned = (direction == Signal.LONG and htf_1h == \"long\") or (\n        direction == Signal.SHORT and htf_1h == \"short\"\n    )\n\n    atr_v",
        )
    if "book_spread_pct=round" not in text:
        text = text.replace(
            "        spread_pct_val = ((hi - lo) / close) if close > 0 else 0.0\n\n    if atr_v",
            "        spread_pct_val = ((hi - lo) / close) if close > 0 else 0.0\n    if book_spread_pct > 0:\n        spread_pct_val = max(spread_pct_val, book_spread_pct)\n\n    if atr_v",
        )
        text = text.replace(
            "        htf_15m_aligned=htf_15m_aligned,\n        votes=votes,",
            "        htf_15m_aligned=htf_15m_aligned,\n        htf_1h_aligned=htf_1h_aligned,\n        book_spread_pct=round(book_spread_pct, 5),\n        votes=votes,",
        )
    path.write_text(text, encoding="utf-8")
    print("patched ta_confluence.py")


def patch_pick_engine() -> None:
    path = ROOT / "pick_engine.py"
    text = path.read_text(encoding="utf-8")
    if "candle_close_confirmed" not in text:
        text = text.replace(
            "    if spread_pct > max_spread:\n        return PickVerdict(False, winner_score, f\"vol gate spread {spread_pct:.3%}\")\n\n    # Pullback wait",
            '''    if spread_pct > max_spread:
        return PickVerdict(False, winner_score, f"vol gate spread {spread_pct:.3%}")

    # Session hour gate
    try:
        from winner_intel import session_hour_blocked

        blocked, reason = session_hour_blocked(settings.state_dir)
        if blocked and winner_tier not in ("elite", "apex"):
            return PickVerdict(False, winner_score, f"session block {reason}")
    except Exception:
        pass

    # Pullback wait''',
        )
        text = text.replace(
            "    # 15m HTF structure confirmation\n    htf_15m = getattr(cf, \"htf_15m_aligned\", cf.htf_aligned)",
            '''    # 1H HTF structure confirmation
    htf_1h = getattr(cf, "htf_1h_aligned", getattr(cf, "htf_15m_aligned", cf.htf_aligned))
    if not htf_1h and winner_tier not in ("elite", "apex"):
        if ml_edge < 0.10:
            return PickVerdict(
                False,
                winner_score,
                f"1h HTF misaligned — need ML edge (have {ml_edge:.2f})",
            )

    # 15m HTF structure confirmation
    htf_15m = getattr(cf, "htf_15m_aligned", cf.htf_aligned)''',
        )
        text = text.replace(
            "    min_pick = max(\n        getattr(settings, \"pick_min_score\", 0.62),\n        REGIME_MIN_PICK.get(cf.regime, 0.55),\n    )",
            '''    from winner_intel import regime_floor_adjustment

    base_regime_floor = REGIME_MIN_PICK.get(cf.regime, 0.55)
    regime_floor = regime_floor_adjustment(settings.state_dir, cf.regime, base_regime_floor)
    min_pick = max(getattr(settings, "pick_min_score", 0.62), regime_floor)''',
        )
        text = text.replace(
            "    if ml_ctx.ready and side == Signal.SHORT and ml_ctx.short_precision < 0.42:",
            '''    # 1m candle-close confirmation
    try:
        from winner_intel import candle_close_confirmed

        ohlcv_probe = getattr(settings, "_entry_ohlcv_1m", None)
        ok_candle, why_candle = candle_close_confirmed(
            ohlcv_probe,
            side.value,
            fast_ema=decision.fast_ema,
        )
        if not ok_candle and winner_tier not in ("elite", "apex"):
            bypass = ml_edge > 0.10
            if not bypass:
                return PickVerdict(False, winner_score, f"candle gate {why_candle}", fast_win=fast)
    except Exception:
        pass

    if ml_ctx.ready and side == Signal.SHORT and ml_ctx.short_precision < 0.42:''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched pick_engine.py")


def patch_signals() -> None:
    path = ROOT / "signals.py"
    text = path.read_text(encoding="utf-8")
    if "ohlcv_1h" not in text:
        text = text.replace(
            "    ohlcv_15m = ex.fetch_ohlcv(symbol, \"15m\", 40)\n    funding = ex.fetch_funding_rate(symbol)",
            "    ohlcv_15m = ex.fetch_ohlcv(symbol, \"15m\", 40)\n    ohlcv_1h = ex.fetch_ohlcv(symbol, \"1H\", 30)\n    funding = ex.fetch_funding_rate(symbol)",
        )
    if "book_spread" not in text:
        text = text.replace(
            "    cf = run_all_analyses(\n        ohlcv_1m,\n        ohlcv_5m,\n        ohlcv_15m=ohlcv_15m,\n        funding_rate=funding,",
            '''    book_spread = 0.0
    try:
        from winner_intel import book_spread_pct

        book_spread = book_spread_pct(ex, symbol)
    except Exception:
        pass
    settings._entry_ohlcv_1m = ohlcv_1m

    cf = run_all_analyses(
        ohlcv_1m,
        ohlcv_5m,
        ohlcv_15m=ohlcv_15m,
        ohlcv_1h=ohlcv_1h,
        book_spread_pct=book_spread,
        funding_rate=funding,''',
        )
    if "decision.ml_edge" not in text:
        text = text.replace(
            "    decision.pick_score = pick.score\n    decision.fast_win_score = pick.fast_win",
            '''    decision.pick_score = pick.score
    decision.fast_win_score = pick.fast_win
    try:
        from forward_pick import ml_direction_edge

        decision.ml_edge = ml_direction_edge(ml_ctx, decision.signal)
    except Exception:
        decision.ml_edge = 0.0''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched signals.py")


def patch_ml_outcomes() -> None:
    path = ROOT / "ml/outcomes.py"
    text = path.read_text(encoding="utf-8")
    if "p_long:" not in text and "pick_score:" in text:
        text = text.replace(
            "        pick_score: float | None = None,\n        curve_phase: str = \"\",",
            "        pick_score: float | None = None,\n        p_long: float | None = None,\n        p_short: float | None = None,\n        ml_edge: float | None = None,\n        regime: str = \"\",\n        curve_phase: str = \"\",",
        )
        text = text.replace(
            '            "pick_score": round(float(pick_score), 4) if pick_score is not None else None,\n            "curve_phase": curve_phase or "",',
            '''            "pick_score": round(float(pick_score), 4) if pick_score is not None else None,
            "p_long": round(float(p_long), 4) if p_long is not None else None,
            "p_short": round(float(p_short), 4) if p_short is not None else None,
            "ml_edge": round(float(ml_edge), 4) if ml_edge is not None else None,
            "regime": regime or "",
            "curve_phase": curve_phase or "",''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched ml/outcomes.py")


def patch_bot() -> None:
    path = ROOT / "bot.py"
    text = path.read_text(encoding="utf-8")
    if "is_probe" not in text:
        text = text.replace(
            "    entry_contracts = plan.contracts\n    try:\n        from quality_pick import quality_pick_active",
            "    entry_contracts = plan.contracts\n    is_probe = False\n    full_contracts = plan.contracts\n    try:\n        from quality_pick import quality_pick_active",
        )
        text = text.replace(
            "                if probed >= min_sz and probed < entry_contracts:\n                    entry_contracts = probed\n                    log.info(",
            "                if probed >= min_sz and probed < entry_contracts:\n                    is_probe = True\n                    full_contracts = entry_contracts\n                    entry_contracts = probed\n                    log.info(",
        )
        text = text.replace(
            "    result = ex.open_position(\n        symbol=symbol,\n        side=decision.signal.value,\n        contracts=entry_contracts,\n        stop_pct=plan.stop_pct,\n        take_pct=plan.take_pct,\n        dry_run=settings.dry_run,\n        leverage=plan.leverage,\n    )",
            "    result = ex.open_position(\n        symbol=symbol,\n        side=decision.signal.value,\n        contracts=entry_contracts,\n        stop_pct=plan.stop_pct,\n        take_pct=plan.take_pct,\n        dry_run=settings.dry_run,\n        leverage=plan.leverage,\n        try_maker=bool(getattr(settings, \"maker_entry_enabled\", True)),\n    )",
        )
        text = text.replace(
            "        contracts=plan.contracts,\n        trade_style=tpsl_pol.style,\n    )",
            "        contracts=entry_contracts,\n        trade_style=tpsl_pol.style,\n        is_probe=is_probe,\n        full_contracts=full_contracts if is_probe else entry_contracts,\n    )",
        )
        text = text.replace(
            "                    pick_score=float(getattr(decision, \"pick_score\", 0.0) or 0.0),\n                    curve_phase=curve.curve_phase if curve else \"\",",
            '''                    pick_score=float(getattr(decision, "pick_score", 0.0) or 0.0),
                    p_long=float(getattr(decision, "p_long", 0.0) or getattr(ml_ctx, "p_long", 0.0) if "ml_ctx" in dir() else 0.0),
                    p_short=float(getattr(decision, "p_short", 0.0) or 0.0),
                    ml_edge=float(getattr(decision, "ml_edge", 0.0) or 0.0),
                    regime=str(getattr(decision, "regime", "") or ""),
                    curve_phase=curve.curve_phase if curve else "",''',
        )
    # Fix bot ml_ctx reference - try_open doesn't have ml_ctx in scope easily. Use decision attributes instead.
    text = text.replace(
        "                    p_long=float(getattr(decision, \"p_long\", 0.0) or getattr(ml_ctx, \"p_long\", 0.0) if \"ml_ctx\" in dir() else 0.0),",
        "                    p_long=float(getattr(decision, \"p_long\", 0.0) or 0.0),",
    )
    if "select_tiered_opens" not in text:
        text = text.replace(
            "        elite = select_conviction_ties(\n            ranked,\n            max_opens=per_tick,\n            min_conviction=mission_floor,\n            apex_preferred=settings.winner_apex_preferred and not entry_press,\n            elite_only=settings.winner_elite_only,\n            allow_elite_fallback=allow_apex_fallback,\n        )",
            '''        try:
            from winner_intel import apply_correlation_ranking, select_tiered_opens

            ranked = apply_correlation_ranking(ranked)
            if getattr(settings, "tiered_slot_policy", True) and quality_first:
                elite = select_tiered_opens(ranked, max_opens=per_tick, min_conviction=mission_floor)
            else:
                elite = select_conviction_ties(
                    ranked,
                    max_opens=per_tick,
                    min_conviction=mission_floor,
                    apex_preferred=settings.winner_apex_preferred and not entry_press,
                    elite_only=settings.winner_elite_only,
                    allow_elite_fallback=allow_apex_fallback,
                )
        except Exception:
            elite = select_conviction_ties(
                ranked,
                max_opens=per_tick,
                min_conviction=mission_floor,
                apex_preferred=settings.winner_apex_preferred and not entry_press,
                elite_only=settings.winner_elite_only,
                allow_elite_fallback=allow_apex_fallback,
            )''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched bot.py")


def patch_position_registry() -> None:
    path = ROOT / "position_registry.py"
    text = path.read_text(encoding="utf-8")
    if "is_probe" not in text:
        text = text.replace(
            "        trade_style: str | None = None,\n    ) -> None:",
            "        trade_style: str | None = None,\n        is_probe: bool = False,\n        full_contracts: float | None = None,\n    ) -> None:",
        )
        text = text.replace(
            "        if contracts is not None and contracts > 0:\n            row[\"contracts\"] = float(contracts)\n        self._data[symbol] = row",
            '''        if contracts is not None and contracts > 0:
            row["contracts"] = float(contracts)
        if is_probe:
            row["is_probe"] = True
            row["probe_state"] = "pending"
            row["full_contracts"] = float(full_contracts or contracts or 0)
        self._data[symbol] = row''',
        )
        text = text.replace(
            "    def update_tpsl(",
            '''    def update_probe(self, symbol: str, *, state: str, contracts: float | None = None) -> None:
        row = self._data.get(symbol)
        if not row:
            return
        row["probe_state"] = state
        if contracts is not None:
            row["contracts"] = float(contracts)
        row["is_probe"] = state not in ("confirmed", "failed", "scaled")
        self._save()

    def update_tpsl(''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched position_registry.py")


def patch_exchange_client() -> None:
    path = ROOT / "exchange_client.py"
    text = path.read_text(encoding="utf-8")
    if "try_maker" not in text:
        text = text.replace(
            "from blofin_http import BlofinHttp",
            "from blofin_http import BlofinHttp, extract_order_id",
        )
        text = text.replace(
            "        leverage: int | None = None,\n    ) -> dict[str, Any] | None:",
            "        leverage: int | None = None,\n        try_maker: bool = False,\n    ) -> dict[str, Any] | None:",
        )
        insert = '''        if try_maker and not dry_run and price > 0:
            maker_fill = self._enter_maker_then_market(
                inst_id=inst_id,
                symbol=symbol,
                side=side,
                contracts=size_f,
                min_size=min_size,
                price=price,
                position_side=position_side,
                order_side=order_side,
                margin_mode=margin_mode,
                leverage=lev,
            )
            if maker_fill is not None:
                time.sleep(0.35)
                pos_after = self._lookup_open_position(symbol, side)
                if pos_after:
                    self._attach_tpsl_after_fill(symbol, side, pos_after, stop_pct, take_pct, lev)
                    return maker_fill

'''
        text = text.replace(
            "        if dry_run:\n            return None\n\n        self.ensure_account_margin_mode()",
            insert + "        if dry_run:\n            return None\n\n        self.ensure_account_margin_mode()",
        )
    if "def _enter_maker_then_market" not in text:
        text += '''

    def _enter_maker_then_market(
        self,
        *,
        inst_id: str,
        symbol: str,
        side: str,
        contracts: float,
        min_size: float,
        price: float,
        position_side: str,
        order_side: str,
        margin_mode: str,
        leverage: int,
        wait_sec: float = 4.0,
    ) -> dict[str, Any] | None:
        """Post-only limit near touch; fall back to market if not filled."""
        import time as _time

        offset = 0.0001 if order_side == "buy" else -0.0001
        limit_px = price * (1.0 + offset)
        size_str = _quantize_order_size(contracts, min_size)
        maker_body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "side": order_side,
            "orderType": "limit",
            "price": str(round(limit_px, 8)),
            "size": size_str,
            "brokerId": self.settings.broker_id,
            "postOnly": True,
        }
        try:
            resp = self.http.place_order(maker_body)
            order_id = extract_order_id(resp)
            if not order_id:
                return None
            deadline = _time.time() + wait_sec
            while _time.time() < deadline:
                pos = self._lookup_open_position(symbol, side)
                if pos and float(pos.get("contracts") or 0) > 0:
                    log.info("MAKER fill %s %s", symbol.split("/")[0], side)
                    return resp
                _time.sleep(0.4)
            self.http.cancel_order(inst_id, order_id)
            log.info("MAKER timeout %s — market fallback", symbol.split("/")[0])
        except Exception as exc:
            log.debug("maker entry failed %s: %s", symbol.split("/")[0], exc)
        return None

    def _attach_tpsl_after_fill(self, symbol, side, pos_after, stop_pct, take_pct, lev):
        try:
            entry = float(pos_after.get("entry_price") or pos_after.get("avgPx") or 0)
            contracts = float(pos_after.get("contracts") or 0)
            if entry > 0 and contracts > 0:
                self.repair_position_tpsl(
                    symbol, side, contracts,
                    take_pct=take_pct, configured_leverage=lev, dry_run=False,
                )
        except Exception:
            pass
'''
    path.write_text(text, encoding="utf-8")
    print("patched exchange_client.py")


def patch_position_steward() -> None:
    path = ROOT / "position_steward.py"
    text = path.read_text(encoding="utf-8")
    if "def manage_probe_positions" not in text:
        steward_funcs = '''

def _position_roe_pct(side: str, entry: float, last: float, leverage: int = 10) -> float:
    gross = _gross_pnl_pct(side, entry, last)
    return gross * leverage * 100.0


def manage_probe_positions(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: PositionRegistry,
    engine: "AutonomousGrowthEngine",
    tracker: "TradeOutcomeTracker | None",
) -> int:
    """Scale probe entries up on confirmation or cut fast on failure."""
    changed = 0
    for sym, pos in list(positions.items()):
        meta = registry.get(sym) or {}
        if not meta.get("is_probe") or meta.get("probe_state") not in ("pending", None):
            continue
        opened = float(meta.get("opened_at") or 0)
        age = time.time() - opened if opened else 0
        if age < 55:
            continue
        trade_sym = str(pos.get("symbol") or sym).split("#")[0]
        side = str(pos.get("side") or "")
        entry = float(pos.get("entry_price") or 0)
        last = (ex.stream.get_last_price(trade_sym) if ex.stream else None) or entry
        lev = int(pos.get("leverage") or meta.get("leverage") or settings.leverage)
        roe = _position_roe_pct(side, entry, last, lev)
        full = float(meta.get("full_contracts") or 0)
        cur = float(pos.get("contracts") or meta.get("contracts") or 0)
        if roe <= -1.0 and age >= 60:
            try:
                ex.close_position(trade_sym, pos, dry_run=False)
                registry.update_probe(trade_sym, state="failed")
                registry.remove(trade_sym)
                positions.pop(sym, None)
                changed += 1
                log.info("PROBE FAIL-CUT %s roe=%.1f%%", trade_sym.split("/")[0], roe)
            except Exception:
                log.debug("probe fail-cut error", exc_info=True)
            continue
        if roe >= 0.3 and full > cur and age >= 75:
            add = max(0.0, full - cur)
            market = ex.market_for(trade_sym)
            min_sz = market.min_size if market else 0.01
            if add >= min_sz:
                try:
                    ex.open_position(
                        trade_sym, side, add,
                        stop_pct=float(meta.get("stop_pct") or 0.012),
                        take_pct=float(meta.get("take_pct") or 0.036),
                        dry_run=settings.dry_run,
                        leverage=lev,
                        try_maker=False,
                    )
                    registry.update_probe(trade_sym, state="scaled", contracts=full)
                    changed += 1
                    log.info("PROBE SCALE-UP %s +%.4f roe=%.1f%%", trade_sym.split("/")[0], add, roe)
                except Exception:
                    log.debug("probe scale-up error", exc_info=True)
    return changed


def manage_smart_exits(
    ex: "BlofinExchange",
    settings: "Settings",
    positions: dict,
    registry: PositionRegistry,
    engine: "AutonomousGrowthEngine",
    tracker: "TradeOutcomeTracker | None",
) -> int:
    """Breakeven stop move, chop exit, partial TP at 1R."""
    closed = 0
    for sym, pos in list(positions.items()):
        meta = registry.get(sym) or {}
        trade_sym = str(pos.get("symbol") or sym).split("#")[0]
        side = str(pos.get("side") or "")
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            continue
        last = (ex.stream.get_last_price(trade_sym) if ex.stream else None) or entry
        lev = int(pos.get("leverage") or meta.get("leverage") or settings.leverage)
        roe = _position_roe_pct(side, entry, last, lev)
        opened = float(meta.get("opened_at") or 0)
        age = time.time() - opened if opened else 0
        stop_pct = float(meta.get("stop_pct") or pos.get("stop_pct") or 0.012)
        one_r_roe = stop_pct * lev * 100.0

        if age >= 90 and roe >= 1.2 and not meta.get("breakeven_moved"):
            meta["breakeven_moved"] = True
            registry._data[trade_sym] = meta
            registry._save()
            log.info("BREAKEVEN armed %s roe=%.1f%%", trade_sym.split("/")[0], roe)

        path_eff = float(meta.get("path_efficiency") or pos.get("path_efficiency") or 0.5)
        if age >= 120 and roe < 0 and path_eff < 0.25:
            try:
                ex.close_position(trade_sym, pos, dry_run=False)
                registry.remove(trade_sym)
                positions.pop(sym, None)
                closed += 1
                log.info("CHOP EXIT %s path=%.0f%% roe=%.1f%%", trade_sym.split("/")[0], path_eff * 100, roe)
            except Exception:
                pass
            continue

        if not meta.get("partial_tp_done") and roe >= one_r_roe * 0.95 and one_r_roe > 0:
            contracts = float(pos.get("contracts") or 0)
            market = ex.market_for(trade_sym)
            min_sz = market.min_size if market else 0.01
            close_sz = max(min_sz, contracts * 0.4)
            if contracts > close_sz + min_sz * 0.5:
                try:
                    ex.partial_close_position(trade_sym, side, close_sz, dry_run=settings.dry_run)
                    meta["partial_tp_done"] = True
                    registry._data[trade_sym] = meta
                    registry._save()
                    log.info("PARTIAL TP 1R %s closed %.0f%% roe=%.1f%%", trade_sym.split("/")[0], 40, roe)
                except Exception:
                    log.debug("partial tp failed", exc_info=True)
    return closed
'''
        text = text.replace(
            "def harvest_all_mature(",
            steward_funcs + "\ndef harvest_all_mature(",
        )
        text = text.replace(
            "    harvested = harvest_all_mature(\n        ex, settings, positions, registry, tracker, engine, harvest_eagerness\n    )",
            "    manage_probe_positions(ex, settings, positions, registry, engine, tracker)\n    smart_closed = manage_smart_exits(ex, settings, positions, registry, engine, tracker)\n    if smart_closed:\n        positions = ex.fetch_all_positions()\n        positions = enrich_positions(positions, registry)\n    harvested = harvest_all_mature(\n        ex, settings, positions, registry, tracker, engine, harvest_eagerness\n    )",
        )
    path.write_text(text, encoding="utf-8")
    print("patched position_steward.py")


def patch_conviction() -> None:
    path = ROOT / "conviction.py"
    text = path.read_text(encoding="utf-8")
    if "weak_wr_blend" not in text:
        text = text.replace(
            "def conviction_score(decision: StrategyDecision, path_reliability: float) -> float:\n    \"\"\"Higher = stronger edge",
            '''def conviction_score(decision: StrategyDecision, path_reliability: float, *, weak_wr_blend: bool = False) -> float:
    """Higher = stronger edge''',
        )
        text = text.replace(
            "    if ps >= 0.62:\n        conv = min(1.0, max(conv, ps * 0.97))\n    return conv",
            '''    if ps >= 0.62:
        conv = min(1.0, max(conv, ps * 0.97))
    if weak_wr_blend:
        fast_w = float(getattr(decision, "fast_win_score", 0.0) or 0.0)
        ml_edge = float(getattr(decision, "ml_edge", 0.0) or 0.0)
        blend = 0.45 * ps + 0.35 * fast_w + 0.20 * max(0.0, ml_edge)
        conv = max(conv, min(1.0, blend))
    return conv''',
        )
        text = text.replace(
            "        conv = conviction_score(dec, path_reliability)\n        if mission_scale:",
            '''        weak_blend = False
        try:
            from winner_intel import optimizer_loosen_frozen
            from config import load_settings

            weak_blend = optimizer_loosen_frozen(load_settings())
        except Exception:
            pass
        conv = conviction_score(dec, path_reliability, weak_wr_blend=weak_blend)
        if mission_scale:''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched conviction.py")


def patch_optimizer_freeze() -> None:
    so = ROOT / "scalp_optimizer.py"
    text = so.read_text(encoding="utf-8")
    if "optimizer_loosen_frozen" not in text:
        text = text.replace(
            "        if starved and eq15 > -2.5:\n            if not pace_only:",
            '''        try:
            from winner_intel import optimizer_loosen_frozen

            freeze_loosen = optimizer_loosen_frozen(self.settings)
        except Exception:
            freeze_loosen = quality_first

        if starved and eq15 > -2.5:
            if not pace_only:''',
        )
        text = text.replace(
            "            else:\n                t.confluence_delta = max(-0.10, t.confluence_delta - 0.018)",
            "            elif freeze_loosen:\n                action = \"pace_up_quality_frozen\"\n                notes.append(\"starved but gates frozen (quality/weak WR)\")\n            else:\n                t.confluence_delta = max(-0.10, t.confluence_delta - 0.018)",
        )
    so.write_text(text, encoding="utf-8")

    tg = ROOT / "throughput_guard.py"
    ttext = tg.read_text(encoding="utf-8")
    if "optimizer_loosen_frozen" not in ttext:
        ttext = ttext.replace(
            "    step = 0.012 if severity == \"mild\" else 0.022\n\n    t.confluence_delta",
            '''    step = 0.012 if severity == "mild" else 0.022
    try:
        from winner_intel import optimizer_loosen_frozen

        if optimizer_loosen_frozen(settings):
            actions.append("throughput_nudge_skipped_quality_freeze")
            return
    except Exception:
        pass

    t.confluence_delta''',
        )
    tg.write_text(ttext, encoding="utf-8")
    print("patched optimizer freeze")


def patch_trade_lessons() -> None:
    path = ROOT / "trade_lessons.py"
    text = path.read_text(encoding="utf-8")
    if "punish_chase" not in text:
        text = text.replace(
            '    "stress_caution",\n)',
            '    "stress_caution",\n    "punish_chase",\n    "punish_wide_spread",\n    "punish_bad_session",\n)',
        )
        text = text.replace(
            "        if \"sl\" in reason.lower():\n            negative.append(\"stop_hit\")",
            '''        if "sl" in reason.lower():
            negative.append("stop_hit")
        chase = float(record.get("vwap_distance_pct") or record.get("chase_pct") or 0)
        if abs(chase) > 0.012:
            negative.append(f"chase_entry {chase:.2%}")
            tags.append("punish_chase")
        spread = float(record.get("spread_pct") or record.get("book_spread_pct") or 0)
        if spread > 0.0012:
            negative.append(f"wide_spread {spread:.3%}")
            tags.append("punish_wide_spread")''',
        )
        text = text.replace(
            "def entry_blocked_by_lessons(\n    settings: \"Settings\",\n    symbol: str,\n    side: str,\n    *,\n    run_label: str = \"\",\n    is_choppy: bool = False,\n) -> tuple[bool, str]:",
            '''def entry_blocked_by_lessons(
    settings: "Settings",
    symbol: str,
    side: str,
    *,
    run_label: str = "",
    is_choppy: bool = False,
    chase_pct: float = 0.0,
    spread_pct: float = 0.0,
) -> tuple[bool, str]:''',
        )
        text = text.replace(
            "    ok, reason = pattern_blocked(settings, symbol, run_label=run_label, is_choppy=is_choppy)\n    if ok:\n        return True, f\"lesson pattern: {reason}\"\n    return False, \"\"",
            '''    ok, reason = pattern_blocked(settings, symbol, run_label=run_label, is_choppy=is_choppy)
    if ok:
        return True, f"lesson pattern: {reason}"
    active = _load_active(settings.state_dir)
    now = time.time()
    for key, block in (active.get("pattern_blocks") or {}).items():
        if float(block.get("until", 0)) <= now:
            continue
        if key == "chase" and abs(chase_pct) > 0.008:
            return True, str(block.get("reason") or "chase lesson block")
        if key == "spread" and spread_pct > 0.001:
            return True, str(block.get("reason") or "spread lesson block")
    return False, ""''',
        )
        text = text.replace(
            '        pb[key] = {"until": time.time() + 2400.0, "reason": "choppy_loss"}',
            '''        pb[key] = {"until": time.time() + 2400.0, "reason": "choppy_loss"}
    if lesson.outcome == "loss" and "punish_chase" in lesson.tags:
        pb = active.setdefault("pattern_blocks", {})
        pb["chase"] = {"until": time.time() + 1800.0, "reason": "chase_loss"}
    if lesson.outcome == "loss" and "punish_wide_spread" in lesson.tags:
        pb = active.setdefault("pattern_blocks", {})
        pb["spread"] = {"until": time.time() + 1200.0, "reason": "wide_spread_loss"}''',
        )
    path.write_text(text, encoding="utf-8")
    print("patched trade_lessons.py")


def patch_winner_gate() -> None:
    path = ROOT / "winner_gate.py"
    text = path.read_text(encoding="utf-8")
    if "chase_pct=" not in text:
        text = text.replace(
            "            blocked, reason = entry_blocked_by_lessons(\n                settings,\n                symbol,\n                side.value if hasattr(side, \"value\") else str(side),\n                run_label=str(getattr(cf, \"run_label\", \"\") or \"\"),\n                is_choppy=bool(getattr(cf, \"is_choppy\", False)),\n            )",
            "            blocked, reason = entry_blocked_by_lessons(\n                settings,\n                symbol,\n                side.value if hasattr(side, \"value\") else str(side),\n                run_label=str(getattr(cf, \"run_label\", \"\") or \"\"),\n                is_choppy=bool(getattr(cf, \"is_choppy\", False)),\n                chase_pct=float(getattr(cf, \"vwap_distance_pct\", 0.0) or 0.0),\n                spread_pct=float(getattr(cf, \"spread_pct\", 0.0) or getattr(cf, \"book_spread_pct\", 0.0) or 0.0),\n            )",
        )
    path.write_text(text, encoding="utf-8")
    print("patched winner_gate.py")


def main() -> None:
    patch_blofin_http()
    patch_market_stream()
    patch_ta_confluence()
    patch_pick_engine()
    patch_signals()
    patch_ml_outcomes()
    patch_bot()
    patch_position_registry()
    patch_exchange_client()
    patch_position_steward()
    patch_conviction()
    patch_optimizer_freeze()
    patch_trade_lessons()
    patch_winner_gate()
    print("all winner ideas patched")


if __name__ == "__main__":
    main()
