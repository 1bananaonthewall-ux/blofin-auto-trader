#!/usr/bin/env python3
"""Pre-flight smoke test — run before starting bot.py live."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def ok(name: str) -> None:
    print(f"  [OK] {name}")


def fail(name: str, err: Exception) -> None:
    FAILURES.append(f"{name}: {err}")
    print(f"  [FAIL] {name}: {err}")


def test_imports() -> None:
    print("=== Imports ===")
    modules = [
        "autonomous_engine",
        "fluid_manifold",
        "pnl_curve",
        "scan_orchestrator",
        "position_steward",
        "mission_brain",
        "mission_config",
        "market_stream",
        "conviction",
        "position_rotator",
        "position_registry",
        "entry_pacer",
        "margin_engine",
        "exchange_client",
        "signals",
        "ml.features",
        "ml.trainer",
        "ml.predictor",
        "bot",
        "self_heal",
        "scalp_profile",
        "liquidation_guard",
        "account_guard",
    ]
    for m in modules:
        try:
            __import__(m)
            ok(m)
        except Exception as e:
            fail(m, e)
            traceback.print_exc()


def test_conviction_ties() -> None:
    print("\n=== Conviction ties ===")
    from conviction import RankedSetup, select_conviction_ties
    from strategy import Signal, StrategyDecision

    def dec(score: float, conf: float) -> StrategyDecision:
        return StrategyDecision(
            signal=Signal.LONG,
            score=score,
            fast_ema=1,
            slow_ema=1,
            rsi=50,
            close=100,
            stop_pct=0.01,
            take_pct=0.02,
            volume_ratio=1,
            htf_aligned=True,
            funding_rate=None,
            model_confidence=conf,
        )

    ranked = [
        RankedSetup("A", dec(80, 0.80), 0.80, 0.80, 80),
        RankedSetup("B", dec(78, 0.79), 0.79, 0.79, 78),
        RankedSetup("C", dec(77, 0.78), 0.78, 0.78, 77),
        RankedSetup("D", dec(60, 0.60), 0.60, 0.60, 60),
    ]
    elite = select_conviction_ties(ranked, max_opens=3)
    assert len(elite) == 3, f"expected 3 ties got {len(elite)}"
    assert elite[-1].symbol == "C"
    assert "D" not in [e.symbol for e in elite]
    ok(f"3-way tie selection ({', '.join(e.symbol for e in elite)})")


def test_mission_brain() -> None:
    print("\n=== Mission brain ===")
    from growth_optimizer import CompoundGrowthOptimizer, GrowthMetrics
    from mission_brain import MissionBrain, SOLE_OBJECTIVE
    from pnl_curve import PnlCurveEngine

    brain = MissionBrain()
    assert "10%" in SOLE_OBJECTIVE and "exceed" in SOLE_OBJECTIVE.lower()
    with tempfile.TemporaryDirectory() as td:
        pnl = PnlCurveEngine(Path(td))
        curve = pnl.update(40.0, 3.0)
        metrics = GrowthMetrics(
            current_equity=40.0,
            days_remaining=469,
            required_daily_return_pct=3.18,
            required_daily_return_multiplier=1.0318,
            on_track=False,
            projected_capital_at_target=500.0,
            aggression_boost=1.2,
            days_to_double_at_current_rate=-1,
        )
        st = brain.evaluate(40.0, metrics, curve, path_reliability=0.5, survival=0.5)
        assert st.sole_objective == SOLE_OBJECTIVE
        assert st.behind_schedule
        allowed, msg = brain.permits_trade(0.75, st)
        assert allowed, msg
        blocked, _ = brain.permits_trade(0.40, st)
        assert not blocked
        ok(f"directive={st.directive[:40]}...")


def test_ml_training_cap() -> None:
    print("\n=== ML training universe ===")
    from types import SimpleNamespace
    from universe import training_symbol_cap

    s_all = SimpleNamespace(trade_universe="all", ml_train_symbols=60)
    assert training_symbol_cap(s_all) == 0
    s_cap = SimpleNamespace(trade_universe="single", ml_train_symbols=25)
    assert training_symbol_cap(s_cap) == 25
    ok("TRADE_UNIVERSE=all forces full exchange training (ignores ML_TRAIN_SYMBOLS=60)")


def test_position_steward() -> None:
    print("\n=== Position steward ===")
    from position_steward import adopt_exchange_positions, steward_interval_seconds
    from position_registry import PositionRegistry

    with tempfile.TemporaryDirectory() as td:
        reg = PositionRegistry(Path(td))
        pos = {
            "FOO/USDT:USDT": {
                "side": "long",
                "contracts": 1.0,
                "entry_price": 100.0,
            }
        }
        n = adopt_exchange_positions(reg, pos)
        assert n == 1
        assert reg.get("FOO/USDT:USDT") is not None
        ok(f"adopted={n} interval_open={steward_interval_seconds(3):.1f}s")


def test_scan_orchestrator() -> None:
    print("\n=== Scan orchestrator ===")
    from scan_orchestrator import ScanOrchestrator
    from types import SimpleNamespace

    orch = ScanOrchestrator()
    universe = [f"SYM{i}/USDT:USDT" for i in range(500)]
    knobs = SimpleNamespace(
        action_intensity=0.7,
        path_reliability=0.6,
        symbols_per_tick=80,
        preserve_capital=False,
        curve_phase="climbing",
    )
    plan = orch.build_plan(universe, set(), None, knobs)
    assert plan.depth >= 25
    assert plan.depth <= 500
    scan, plan2 = orch.pick_symbols(universe, {"SYM1/USDT:USDT"}, None, knobs)
    assert "SYM1/USDT:USDT" in scan
    ok(f"depth={plan2.depth}/500 rotation={plan2.rotation_offset}")


def test_pnl_curve() -> None:
    print("\n=== PnL curve ===")
    from pnl_curve import PnlCurveEngine

    with tempfile.TemporaryDirectory() as td:
        p = PnlCurveEngine(Path(td))
        ticks = Path(td) / "equity_ticks.jsonl"
        now = time.time()
        with ticks.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 7200, "equity": 10.0}) + "\n")
            fh.write(json.dumps({"ts": now - 3600, "equity": 10.5}) + "\n")
            fh.write(json.dumps({"ts": now, "equity": 11.2}) + "\n")
        st = p.update(11.2, 2.5)
        assert 0 <= st.verticality <= 1
        assert st.curve_phase in ("vertical", "climbing", "flat", "declining")
        ok(f"phase={st.curve_phase} verticality={st.verticality:.2f}")


def test_fluid_manifold() -> None:
    print("\n=== Fluid manifold ===")
    from fluid_manifold import FluidManifold, ManifoldContext

    with tempfile.TemporaryDirectory() as td:
        m = FluidManifold(Path(td))
        snap = m.tick(
            ManifoldContext(
                equity=10.0,
                free_margin=8.0,
                open_count=0,
                win_rate=0.5,
                profit_factor=1.0,
                consecutive_losses=0,
                required_daily_pct=2.0,
                on_track=False,
                days_remaining=400,
                aggression_boost=1.1,
            )
        )
        assert 0 <= snap.action_intensity <= 1
        ok(f"tick action_intensity={snap.action_intensity}")


def test_config_and_credentials() -> None:
    print("\n=== Config ===")
    try:
        from config import load_settings

        s = load_settings()
        ok(f"mode={s.mode} dry_run={s.dry_run} signal={s.signal_mode}")
        if not s.api_key:
            fail("credentials", RuntimeError("missing API key in .env"))
    except Exception as e:
        fail("load_settings", e)


def test_exchange_dry() -> None:
    print("\n=== Exchange (REST) ===")
    try:
        from config import load_settings
        from exchange_client import BlofinExchange

        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        eq = ex.fetch_equity_usdt()
        ok(f"equity=${eq:.4f}")
        inst = ex.list_instruments()
        ok(f"instruments={len(inst)}")
        tickers = ex.list_tickers()
        ok(f"tickers={len(tickers)}")
    except Exception as e:
        fail("exchange REST", e)
        traceback.print_exc()


def test_stream_hub() -> None:
    print("\n=== Market stream ===")
    try:
        from config import load_settings
        from exchange_client import BlofinExchange
        from market_stream import BlofinMarketStream

        settings = load_settings()
        ex = BlofinExchange(settings)
        ex.load()
        inst_ids = [i.get("instId") for i in ex.list_instruments() if (i.get("instId") or "").endswith("-USDT")][:5]
        stream = BlofinMarketStream(ex.http, demo=settings.mode == "demo")
        n = stream.refresh_all_tickers()
        ok(f"REST tickers refreshed: {n}")
        stream.start(inst_ids)
        import time

        time.sleep(2)
        ok("stream threads started")
        stream.stop()
    except Exception as e:
        fail("market stream", e)
        traceback.print_exc()


def test_liquidation_guard() -> None:
    print("\n=== Liquidation guard ===")
    try:
        from liquidation_guard import (
            clamp_stop_take_pct,
            enforce_risk_reward,
            liquidation_distance_pct,
            sl_is_safe,
            sl_tp_from_exchange_liq,
        )

        lev = 50
        liq = liquidation_distance_pct(lev)
        stop, take = clamp_stop_take_pct(0.05, 0.06, lev)
        assert stop < liq, f"stop {stop} must be < liq {liq}"
        entry, liq_px = 100.0, 97.5
        sl, tp, sp, tp_pct = sl_tp_from_exchange_liq("long", entry, liq_px, 0.03)
        assert sl_is_safe("long", entry, sl, liquidation_price=liq_px)
        assert sl > liq_px
        ok(f"exchange-liq SL long sl={sl:.4f} liq={liq_px:.4f}")
        sl3, tp3, sp3, tp3p = sl_tp_from_exchange_liq(
            "long", entry, liq_px, 0.03, min_rr=3.0, enforce_tp_from_sl=True
        )
        rr = tp3p / max(sp3, 1e-9)
        assert abs(rr - 3.0) < 0.05, f"3R repair expected rr~3 got {rr}"
        ok(f"3R exchange-liq TP rr={rr:.2f}:1 stop={sp3*100:.2f}% take={tp3p*100:.2f}%")
        strict = enforce_risk_reward(
            0.014, 0.02, min_rr=3.0, strict=True, max_stop_pct=0.014, max_take_pct=0.048
        )
        assert strict is not None and abs(strict[1] / strict[0] - 3.0) < 0.02
        ok(f"strict 3R enforce stop={strict[0]*100:.2f}% take={strict[1]*100:.2f}%")
    except Exception as e:
        fail("liquidation_guard", e)


def test_self_heal_peak_reset() -> None:
    print("\n=== Self-heal peak reset ===")
    try:
        from autonomous_engine import create_engine
        from self_heal import SelfHealer

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            engine = create_engine(state)
            engine.manifold.peak_equity = 100.0
            engine.pnl._peak = 100.0
            SelfHealer.reset_peaks(engine, 14.0)
            assert abs(engine.manifold.peak_equity - 14.0) < 0.01
            assert abs(engine.pnl._peak - 14.0) < 0.01
            healer = SelfHealer(state, enabled=True)
            engine.activate_recovery(60)
            assert engine._recovery_live()
            ok("peak reset + recovery")
    except Exception as e:
        fail("self_heal", e)


def test_ml_model() -> None:
    print("\n=== ML model ===")
    try:
        from config import load_settings
        from ml.features import FEATURE_NAMES
        from ml.predictor import MLPredictor

        settings = load_settings()
        ml = MLPredictor(settings.state_dir)
        ok(f"features={len(FEATURE_NAMES)}")
        if ml.is_ready():
            ok(ml.metrics_summary())
        else:
            print("  [WARN] model not deployed — run: python train_model.py")
    except Exception as e:
        fail("ML", e)


def test_one_dry_tick() -> None:
    print("\n=== One dry tick ===")
    os.environ["DRY_RUN"] = "true"
    try:
        from config import load_settings
        from autonomous_engine import create_engine
        from bot import run_once
        from cooldowns import SymbolCooldowns
        from entry_pacer import EntryPacer
        from exchange_client import BlofinExchange
        from journal import TradeJournal
        from market_stream import BlofinMarketStream
        from markets import symbol_to_inst_id
        from ml.outcomes import TradeOutcomeTracker
        from ml.predictor import MLPredictor
        from position_registry import PositionRegistry
        from universe import load_tradeable_markets

        settings = load_settings()
        if not settings.dry_run:
            print("  [WARN] forcing DRY_RUN for smoke tick")
        engine = create_engine(settings.state_dir)
        ex = BlofinExchange(settings)
        ex.load()
        mkts = load_tradeable_markets(ex, ex.fetch_equity_usdt(), 10, 0.95, 50)
        ex.refresh_markets(mkts[:50])
        inst_ids = [m.inst_id for m in mkts[:30]]
        stream = BlofinMarketStream(ex.http, demo=settings.mode == "demo")
        stream.start(inst_ids)
        ex.attach_stream(stream)
        import time

        time.sleep(1.5)
        journal = TradeJournal(settings.state_dir / "smoke_trades.jsonl")
        cooldowns = SymbolCooldowns(settings.state_dir / "smoke_cooldowns.json", 60)
        pacer = EntryPacer(settings.state_dir, 75)
        ml = MLPredictor(settings.state_dir)
        tracker = TradeOutcomeTracker(settings.state_dir, 100)
        registry = PositionRegistry(settings.state_dir)
        from symbol_side_guard import SymbolSideGuard

        side_guard = SymbolSideGuard(settings.state_dir, block_seconds=60.0)
        poll = run_once(
            ex, settings, engine, journal, cooldowns, ml, tracker, pacer, registry, side_guard
        )
        ok(f"run_once completed poll={poll}s")
        stream.stop()
    except Exception as e:
        fail("dry tick", e)
        traceback.print_exc()


def test_account_guard() -> None:
    print("\n=== Account guard ===")
    try:
        from account_guard import effective_max_open, entry_allowed, min_free_margin_to_open
        from config import load_settings

        s = load_settings()
        if s.max_positions <= 0 or s.max_positions >= 9999:
            assert effective_max_open(s, 25.0) >= 9999
            allowed, reason = entry_allowed(s, equity=22.0, free_margin=8.0, open_count=50)
            assert allowed, reason
        else:
            assert effective_max_open(s, 25.0) <= max(s.small_account_max_open, s.max_positions)
            assert effective_max_open(s, 100.0) == s.max_positions
            allowed, _ = entry_allowed(s, equity=22.0, free_margin=2.0, open_count=4)
            assert not allowed
            allowed, _ = entry_allowed(s, equity=22.0, free_margin=8.0, open_count=1)
            assert allowed
        ok("small-account caps enforced")
    except Exception as e:
        fail("account_guard", e)


def main() -> int:
    print("Blofin bot pre-flight smoke test\n")
    test_imports()
    test_conviction_ties()
    test_mission_brain()
    test_ml_training_cap()
    test_position_steward()
    test_scan_orchestrator()
    test_pnl_curve()
    test_fluid_manifold()
    test_liquidation_guard()
    test_account_guard()
    test_self_heal_peak_reset()
    test_config_and_credentials()
    test_exchange_dry()
    test_stream_hub()
    test_ml_model()
    test_one_dry_tick()
    print("\n" + "=" * 50)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL SMOKE TESTS PASSED")
    if os.getenv("DRY_RUN", "").lower() not in ("true", "1", "yes"):
        print("\nTip: set DRY_RUN=true in .env for first live session test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
