#!/usr/bin/env python3
"""Account, fluid manifold, and ML snapshot."""

from autonomous_engine import create_engine
from config import load_settings
from exchange_client import BlofinExchange
from ml.outcomes import TradeOutcomeTracker
from ml.predictor import MLPredictor
from self_heal import SelfHealer
from universe import load_tradeable_markets, load_training_markets, training_symbol_cap


def main() -> None:
    settings = load_settings()
    engine = create_engine(settings.state_dir)
    engine.bind_settings(settings)
    engine.unrestricted_trading = settings.unrestricted_trading
    engine.entries_never_pause = settings.entries_never_pause
    ex = BlofinExchange(settings)
    ex.load()
    equity = ex.fetch_equity_usdt()
    free = ex.fetch_free_equity_usdt()
    positions = ex.fetch_all_positions()
    ml = MLPredictor(settings.state_dir, min_confidence=engine.doctrine.min_confidence_floor)
    tracker = TradeOutcomeTracker(settings.state_dir)

    val_acc, long_p, short_p, fb = 0.55, 0.5, 0.5, 0
    if ml.is_ready() and ml.model and ml.model.metrics:
        m = ml.model.metrics
        val_acc, long_p, short_p, fb = m.val_accuracy, m.val_long_precision, m.val_short_precision, m.feedback_samples
    X, y = tracker.load_labelled_samples(200)
    fb = max(fb, len(y))
    engine.set_ml_metrics(val_acc, long_p, short_p, fb)
    engine.update_fluid(equity, free, len(positions))
    knobs = engine.compute_knobs(equity, free, len(positions))
    curve = engine.curve_state

    if settings.trade_all_symbols:
        mkts = load_tradeable_markets(ex, equity, engine.doctrine.base_leverage, 0.95, 9999)
        affordable = len(mkts)
    else:
        affordable = 1

    print(engine.doctrine_summary())
    print(f"~{engine.manifold.parameter_count_estimate:,} decision dimensions (manifold + ML ensemble)")
    healer = SelfHealer(settings.state_dir, enabled=settings.self_heal_enabled)
    heal = healer.summary()
    print(
        f"mode={settings.mode} dry_run={settings.dry_run} "
        f"unrestricted={settings.unrestricted_trading} self_heal={settings.self_heal_enabled} "
        f"scalp={settings.scalp_mode}"
    )
    if settings.scalp_mode:
        line = (
            f"scalp: {settings.scalp_leverage}-{settings.scalp_leverage_max}x "
            f"poll={settings.scalp_poll_seconds}s hold>={settings.scalp_min_hold_seconds:.0f}s "
            f"tp>={settings.scalp_min_take_profit_pct:.2%}"
        )
        if settings.scalp_3r_mode:
            line += (
                f" | 3R profile min_rr={settings.scalp_3r_min_rr:.1f} "
                f"harvest>={settings.scalp_3r_harvest_min_r:.1f}R "
                f"score+={settings.scalp_3r_min_score_bump:.0f} conf+={settings.scalp_3r_min_confidence_bump:.2f}"
            )
        print(line)
    if heal.get("recent_actions"):
        print(f"self_heal: pause_streak={heal['pause_streak']} last={heal['recent_actions']}")
    if engine.recovery_active:
        print("self_heal: recovery_mode ACTIVE")
    print(
        f"fluid: intensity={knobs.action_intensity:.0%} reliability={knobs.path_reliability:.0%} "
        f"survival={knobs.survival:.0%} edge={knobs.edge:.0%} entries={'yes' if knobs.allow_new_entries else 'paused'}"
    )
    print(
        f"knobs: conf>={knobs.min_confidence:.0%} score>={knobs.min_signal_score:.0f} "
        f"risk={knobs.risk_per_trade_pct:.1%} max_lev={knobs.max_leverage} poll={knobs.poll_seconds}s"
    )
    print(f"need {knobs.required_daily_return_pct:.2f}%/day | {knobs.days_remaining} days to target")
    train_cap = training_symbol_cap(settings)
    try:
        train_n = len(load_training_markets(ex, cap=train_cap))
    except Exception:
        train_n = 0
    print(
        f"ml={'ready' if ml.is_ready() else 'not ready'} | {ml.metrics_summary()} | "
        f"train_universe={train_n} ({'all exchange' if train_cap <= 0 else f'cap {train_cap}'})"
    )
    print(f"equity=${equity:.4f} free_margin=${free:.4f} dd={knobs.drawdown_pct:.1f}% affordable={affordable}")
    if curve:
        print(engine.pnl.format_report(curve, equity))
        print(
            f"curve: phase={curve.curve_phase} verticality={curve.verticality:.0%} "
            f"harvest_eagerness={curve.harvest_eagerness:.2f}x entry_scale={curve.entry_scale:.2f}x "
            f"preserve={'yes' if curve.preserve_capital else 'no'}"
        )
    print(f"open_positions={len(positions)}")
    if knobs.drivers:
        print("drivers:", " ".join(knobs.drivers))


if __name__ == "__main__":
    main()
