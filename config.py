import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret: str
    passphrase: str
    mode: str
    trade_universe: str
    symbol: str
    daily_target_pct: float
    stop_on_daily_target: bool
    max_daily_loss_pct: float
    risk_per_trade_pct: float
    max_positions: int
    auto_max_positions: bool
    margin_utilization: float
    min_equity_per_slot: float
    margin_reserve_usdt: float
    symbols_per_tick: int
    min_signal_score: float
    min_volume_ratio: float
    symbol_cooldown_minutes: int
    use_enhanced_strategy: bool
    signal_mode: str
    ml_min_confidence: float
    ml_train_symbols: int
    ml_history_bars: int
    ml_forward_bars: int
    ml_label_threshold: float
    ml_retrain_hours: int
    ml_continuous_train: bool
    ml_bootstrap_symbols: int
    ml_refit_min_shards: int
    ml_refit_interval_minutes: int
    ml_outcome_refit_min_new: int
    ml_auto_refit_on_startup: bool
    ml_min_deploy_samples: int
    cortex_auto_train: bool
    cortex_train_on_startup: bool
    cortex_train_interval_minutes: int
    cortex_train_min_new_closes: int
    # --- walk-forward retraining ---
    ml_walk_forward_splits: int
    ml_walk_forward_min_train: int
    # --- real-fill outcome feedback ---
    ml_real_feedback_max_samples: int
    ml_use_triple_barrier: bool
    ml_barrier_max_bars: int
    ml_purge_gap: int
    ml_embargo_pct: float
    ml_harsh_move_only: bool
    ml_block_weak_longs: bool
    ml_weak_long_precision: float
    pick_min_score: float
    pick_short_horizon_weight: float
    symbol_flip_block_minutes: int
    leverage: int
    poll_seconds: int
    dry_run: bool
    broker_id: str
    state_dir: Path
    log_dir: Path

    # --- Smart sizing for small accounts ---
    fee_est_taker_pct: float          # estimated taker fee (e.g. 0.0006 = 0.06%)
    fee_est_maker_pct: float          # estimated maker fee
    min_take_profit_pct: float        # minimum TP % to cover fees + slippage
    small_account_threshold: float    # equity below this = "small account" mode
    micro_equity_threshold: float     # stricter caps below this (e.g. $3 accounts)
    micro_equity_max_open: int
    micro_drawdown_pause_pct: float   # pause entries after this % drop from session peak
    micro_max_margin_frac: float      # max margin per new open as fraction of equity
    auto_leverage_max: int            # max leverage allowed when auto-scaling
    profit_factor_window: int         # num trades to evaluate profitability
    update_existing_sltp: bool        # update SL/TP on existing positions each tick

    # --- Simple TP/SL with liquidation protection ---
    take_profit_pct: float            # Take profit percentage
    stop_loss_pct: float              # Stop loss percentage
    liquidation_buffer_pct: float     # Buffer from liquidation price (%)
    unrestricted_trading: bool        # skip drawdown/mission/fluid entry pauses
    entries_never_pause: bool       # always allow new entries when margin ok; optimizers still tune quality
    account_curve_maximize: bool      # steer entries/harvest to steepen dashboard account curve
    runner_filter_enabled: bool       # favor steady directional runners, skip choppy up/down
    runner_min_score: float           # minimum runner_score to enter (when filter on)
    runner_max_chop: float            # chop_index above this => reject
    runner_min_path_eff: float        # 1m path efficiency floor
    runner_priority_mode: bool        # rank/enter directional runners ahead of fixed 3R scalps
    runner_extend_take_pct: float     # max TP % for runner momentum (wider than fast 3R cap)
    runner_extend_min_rr: float       # minimum R on runners when extending TP
    runner_trail_enabled: bool        # steward: breakeven + lock profit on runners
    runner_trail_after_r: float       # start trailing after this R multiple
    runner_trail_lock_r: float        # lock this many R once trailing
    runner_require_for_entry: bool    # only open when run_quality says runner
    pick_min_score_runners: float     # lower pick floor for confirmed runners
    universe_fill_mode: bool          # no hourly open cap; fill universe until out of margin
    self_heal_enabled: bool           # auto-recover peaks, ML, entry pauses
    scalp_mode: bool                  # high-lev momentum scalps, fast harvest
    scalp_leverage: int
    scalp_leverage_max: int
    scalp_poll_seconds: int
    scalp_entry_gap_seconds: float
    scalp_min_take_profit_pct: float
    scalp_cooldown_minutes: int
    scalp_atr_stop_mult: float
    scalp_atr_take_mult: float
    scalp_max_stop_pct: float
    scalp_max_take_pct: float
    scalp_min_hold_seconds: float
    scalp_harvest_fee_mult: float
    scalp_steward_interval: float
    scalp_fee_coverage_mult: float
    scalp_3r_mode: bool
    scalp_3r_min_rr: float
    scalp_3r_harvest_min_r: float
    scalp_3r_min_score_bump: float
    scalp_3r_min_confidence_bump: float
    winner_only_mode: bool
    winner_min_confluence: float
    winner_min_agreeing: int
    winner_max_opposing: int
    winner_min_ml_confidence: float
    winner_min_volume_ratio: float
    winner_max_vwap_chase_pct: float
    winner_min_score: float
    winner_elite_score: float
    winner_apex_score: float
    winner_apex_preferred: bool
    winner_apex_starve_minutes: int
    winner_elite_entry_gap_seconds: float
    winner_elite_only: bool
    winner_require_ml_align: bool
    winner_min_ml_margin: float
    winner_min_anchor_votes: int
    winner_max_opposition_ratio: float
    margin_use_fraction: float
    min_margin_rate: float
    target_margin_rate: float
    margin_top_up_enabled: bool
    margin_mode: str  # isolated | cross (Blofin account margin mode)
    auto_cross_margin_migrate: bool
    auto_cross_margin_interval_sec: float
    auto_cross_margin_symbol_cooldown_sec: float
    max_effective_leverage: int
    max_stop_liq_fraction: float
    pre_liquidation_exit_factor: float
    sl_liq_buffer: float
    entries_paused: bool
    max_opens_per_tick: int
    small_account_max_open: int
    small_account_max_opens_per_tick: int
    min_free_margin_pct: float
    small_account_min_free_pct: float
    winner_ml_veto_min_confidence: float
    htf_required: bool
    min_confluence_agreeing: int
    max_same_side_positions: int
    require_ml_model: bool
    max_funding_long: float
    min_funding_short: float
    optimizer_enabled: bool
    optimizer_interval_seconds: float
    optimizer_target_min_tph: int
    optimizer_target_max_tph: int
    optimizer_quality_first: bool
    optimizer_flow_learning_enabled: bool
    optimizer_flow_target_tph: int
    optimizer_flow_lookback_reports: int
    live_update_enabled: bool
    live_update_poll_seconds: float
    live_update_git_pull: bool
    live_update_git_interval_seconds: float
    throughput_brain_enabled: bool
    leverage_rotate_on_start: bool
    leverage_rotate_when_starved: bool
    leverage_rotate_interval_minutes: int
    leverage_auto_upgrade: bool
    stack_winners_mode: bool
    early_harvest_enabled: bool
    moon_swarm_enabled: bool
    markov_regime_enabled: bool
    markov_confidence_boost_trend: float
    markov_confidence_penalty_stress: float
    symbol_quality_enabled: bool
    symbol_quality_floor: float
    exec_slippage_penalty_enabled: bool
    exec_slippage_warn_bps: float
    optimizer_autocode_enabled: bool
    hourly_brain_enabled: bool
    hourly_autocode_enabled: bool
    trade_lessons_enabled: bool
    trade_lesson_ml_refit_every: int
    optimizer_autocode_cooldown_seconds: int
    llm_trading_enabled: bool
    llm_trading_min_confidence: float
    llm_trading_temperature: float
    llm_trading_max_tokens: int
    llm_trading_respect_markov: bool
    llm_trading_fail_open: bool
    llm_trading_min_score: float
    llm_trading_use_cortex: bool
    llm_trading_strict: bool
    llm_only_trading: bool
    llm_policy_cache_sec: float
    momentum_wave_mode: bool
    momentum_wave_target_daily_pct: float
    momentum_wave_target_levered_profit_pct: float
    hourly_3r_winner_mode: bool
    optimizer_target_min_wins_per_hour: int
    tpsl_only_pacing: bool
    tpsl_pace_base_gap_seconds: float
    tpsl_pace_gap_after_tp_seconds: float
    tpsl_pace_gap_after_sl_seconds: float
    tpsl_pace_symbol_sl_cooldown_seconds: float
    scalp_fast_3r: bool
    scalp_fast_max_stop_pct: float
    scalp_fast_max_take_pct: float
    scalp_momentum_max_stop_pct: float
    scalp_skip_liq_tpsl: bool
    scalp_roe_harvest_enabled: bool
    scalp_roe_harvest_min_pct: float
    scalp_roe_harvest_max_pct: float

    @property
    def trade_all_symbols(self) -> bool:
        return self.trade_universe.strip().lower() in {"all", "*", "universe"}


def load_settings() -> Settings:
    api_key = os.getenv("BLOFIN_API_KEY", "").strip()
    secret = os.getenv("BLOFIN_SECRET", "").strip()
    passphrase = os.getenv("BLOFIN_PASSPHRASE", "").strip()
    if not api_key or not secret or not passphrase:
        raise ValueError(
            "Missing BLOFIN_API_KEY, BLOFIN_SECRET, or BLOFIN_PASSPHRASE in .env"
        )

    return Settings(
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
        mode=os.getenv("BLOFIN_MODE", "live").strip().lower(),
        trade_universe=os.getenv("TRADE_UNIVERSE", "all").strip(),
        symbol=os.getenv("SYMBOL", "BTC/USDT:USDT").strip(),
        daily_target_pct=float(os.getenv("DAILY_TARGET_PCT", "0.10")),
        stop_on_daily_target=_env_bool("STOP_ON_DAILY_TARGET", False),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.15")),  # 15% max daily loss
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "0.12")),
        max_positions=int(os.getenv("MAX_POSITIONS", "0")),
        auto_max_positions=_env_bool("AUTO_MAX_POSITIONS", False),
        margin_utilization=float(os.getenv("MARGIN_UTILIZATION_PCT", "0.90")),
        min_equity_per_slot=float(os.getenv("MIN_EQUITY_PER_SLOT", "0.01")),
        margin_reserve_usdt=float(os.getenv("MARGIN_RESERVE_USDT", "0.05")),
        symbols_per_tick=int(os.getenv("SYMBOLS_PER_TICK", "120")),
        min_signal_score=float(os.getenv("MIN_SIGNAL_SCORE", "65")),
        min_volume_ratio=float(os.getenv("MIN_VOLUME_RATIO", "0.8")),
        symbol_cooldown_minutes=int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "15")),  # shorter cooldown
        use_enhanced_strategy=_env_bool("USE_ENHANCED_STRATEGY", True),
        signal_mode=os.getenv("SIGNAL_MODE", "ml").strip().lower(),
        ml_min_confidence=float(os.getenv("ML_MIN_CONFIDENCE", "0.74")),
        ml_train_symbols=int(os.getenv("ML_TRAIN_SYMBOLS", "0")),  # 0 = all; ignored when TRADE_UNIVERSE=all
        ml_history_bars=int(os.getenv("ML_HISTORY_BARS", "1500")),  # deeper history
        ml_forward_bars=int(os.getenv("ML_FORWARD_BARS", "2")),
        ml_label_threshold=float(os.getenv("ML_LABEL_THRESHOLD", "0.0010")),
        ml_retrain_hours=int(os.getenv("ML_RETRAIN_HOURS", "12")),  # retrain more frequently
        ml_continuous_train=_env_bool("ML_CONTINUOUS_TRAIN", True),
        ml_bootstrap_symbols=int(os.getenv("ML_BOOTSTRAP_SYMBOLS", "25")),
        ml_refit_min_shards=int(os.getenv("ML_REFIT_MIN_SHARDS", "12")),
        ml_refit_interval_minutes=int(os.getenv("ML_REFIT_INTERVAL_MINUTES", "20")),
        ml_outcome_refit_min_new=int(os.getenv("ML_OUTCOME_REFIT_MIN_NEW", "3")),
        ml_auto_refit_on_startup=_env_bool("ML_AUTO_REFIT_ON_STARTUP", True),
        ml_min_deploy_samples=int(os.getenv("ML_MIN_DEPLOY_SAMPLES", "350")),
        cortex_auto_train=_env_bool("CORTEX_AUTO_TRAIN", True),
        cortex_train_on_startup=_env_bool("CORTEX_TRAIN_ON_STARTUP", True),
        cortex_train_interval_minutes=int(os.getenv("CORTEX_TRAIN_INTERVAL_MINUTES", "15")),
        cortex_train_min_new_closes=int(os.getenv("CORTEX_TRAIN_MIN_NEW_CLOSES", "1")),
        ml_walk_forward_splits=int(os.getenv("ML_WALK_FORWARD_SPLITS", "10")),  # more splits for robustness
        ml_walk_forward_min_train=int(os.getenv("ML_WALK_FORWARD_MIN_TRAIN", "300")),
        ml_real_feedback_max_samples=int(os.getenv("ML_REAL_FEEDBACK_MAX_SAMPLES", "1000")),
        ml_use_triple_barrier=_env_bool("ML_USE_TRIPLE_BARRIER", True),
        ml_barrier_max_bars=int(os.getenv("ML_BARRIER_MAX_BARS", "30")),
        ml_purge_gap=int(os.getenv("ML_PURGE_GAP", "30")),
        ml_embargo_pct=float(os.getenv("ML_EMBARGO_PCT", "0.01")),
        ml_harsh_move_only=_env_bool("ML_HARSH_MOVE_ONLY", True),
        ml_block_weak_longs=_env_bool("ML_BLOCK_WEAK_LONGS", True),
        ml_weak_long_precision=float(os.getenv("ML_WEAK_LONG_PRECISION", "0.42")),
        pick_min_score=float(os.getenv("PICK_MIN_SCORE", "0.62")),
        pick_short_horizon_weight=float(os.getenv("PICK_SHORT_HORIZON_WEIGHT", "0.55")),
        symbol_flip_block_minutes=int(os.getenv("SYMBOL_FLIP_BLOCK_MINUTES", "20")),
        leverage=int(os.getenv("LEVERAGE", "10")),
        poll_seconds=int(os.getenv("POLL_SECONDS", "30")),
        dry_run=_env_bool("DRY_RUN", True),
        broker_id=os.getenv("BLOFIN_BROKER_ID", "5388cb1f51cec2e3").strip(),
        state_dir=ROOT / "state",
        log_dir=ROOT / "logs",
        fee_est_taker_pct=float(os.getenv("FEE_EST_TAKER_PCT", "0.0006")),
        fee_est_maker_pct=float(os.getenv("FEE_EST_MAKER_PCT", "0.0002")),
        min_take_profit_pct=float(os.getenv("MIN_TAKE_PROFIT_PCT", "0.003")),
        small_account_threshold=float(os.getenv("SMALL_ACCOUNT_THRESHOLD", "50.0")),
        auto_leverage_max=int(os.getenv("AUTO_LEVERAGE_MAX", "25")),
        profit_factor_window=int(os.getenv("PROFIT_FACTOR_WINDOW", "10")),  # shorter window for faster adaptation
        update_existing_sltp=_env_bool("UPDATE_EXISTING_SLTP", True),  # always update SL/TP
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "25.0")),  # 25% take profit
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "8.0")),  # 8% stop loss
        liquidation_buffer_pct=float(os.getenv("LIQUIDATION_BUFFER_PCT", "10.0")),
        unrestricted_trading=_env_bool("UNRESTRICTED_TRADING", False),
        entries_never_pause=_env_bool("ENTRIES_NEVER_PAUSE", True),
        account_curve_maximize=(
            _env_bool("ACCOUNT_CURVE_MAXIMIZE", True)
            if os.getenv("ACCOUNT_CURVE_MAXIMIZE") is not None
            else _env_bool("CURVE_MAXIMIZER", True)
        ),
        runner_filter_enabled=_env_bool("RUNNER_FILTER_ENABLED", True),
        runner_min_score=float(os.getenv("RUNNER_MIN_SCORE", "0.48")),
        runner_max_chop=float(os.getenv("RUNNER_MAX_CHOP", "0.56")),
        runner_min_path_eff=float(os.getenv("RUNNER_MIN_PATH_EFF", "0.26")),
        runner_priority_mode=_env_bool(
            "RUNNER_PRIORITY_MODE",
            _env_bool("MOMENTUM_WAVE_MODE", True),
        ),
        runner_extend_take_pct=float(os.getenv("RUNNER_EXTEND_TAKE_PCT", "0.08")),
        runner_extend_min_rr=float(os.getenv("RUNNER_EXTEND_MIN_RR", "2.5")),
        runner_trail_enabled=_env_bool("RUNNER_TRAIL_ENABLED", True),
        runner_trail_after_r=float(os.getenv("RUNNER_TRAIL_AFTER_R", "1.0")),
        runner_trail_lock_r=float(os.getenv("RUNNER_TRAIL_LOCK_R", "0.55")),
        runner_require_for_entry=_env_bool("RUNNER_REQUIRE_FOR_ENTRY", False),
        pick_min_score_runners=float(os.getenv("PICK_MIN_SCORE_RUNNERS", "0.38")),
        universe_fill_mode=_env_bool("UNIVERSE_FILL_MODE", False),
        micro_equity_threshold=float(os.getenv("MICRO_EQUITY_THRESHOLD", "10.0")),
        micro_equity_max_open=int(os.getenv("MICRO_EQUITY_MAX_OPEN", "3")),
        micro_drawdown_pause_pct=float(os.getenv("MICRO_DRAWDOWN_PAUSE_PCT", "6.0")),
        micro_max_margin_frac=float(os.getenv("MICRO_MAX_MARGIN_FRAC", "0.12")),
        self_heal_enabled=_env_bool("SELF_HEAL", True),
        scalp_mode=_env_bool("SCALP_MODE", True),
        scalp_leverage=int(os.getenv("SCALP_LEVERAGE", "20")),
        scalp_leverage_max=int(os.getenv("SCALP_LEVERAGE_MAX", "50")),
        scalp_poll_seconds=int(os.getenv("SCALP_POLL_SECONDS", "12")),
        scalp_entry_gap_seconds=float(os.getenv("SCALP_ENTRY_GAP_SECONDS", "35")),
        scalp_min_take_profit_pct=float(os.getenv("SCALP_MIN_TAKE_PROFIT_PCT", "0.006")),
        scalp_cooldown_minutes=int(os.getenv("SCALP_COOLDOWN_MINUTES", "5")),
        scalp_atr_stop_mult=float(os.getenv("SCALP_ATR_STOP_MULT", "1.15")),
        scalp_atr_take_mult=float(os.getenv("SCALP_ATR_TAKE_MULT", "2.1")),
        scalp_max_stop_pct=float(os.getenv("SCALP_MAX_STOP_PCT", "0.022")),
        scalp_max_take_pct=float(os.getenv("SCALP_MAX_TAKE_PCT", "0.045")),
        scalp_min_hold_seconds=float(os.getenv("SCALP_MIN_HOLD_SECONDS", "40")),
        scalp_harvest_fee_mult=float(os.getenv("SCALP_HARVEST_FEE_MULT", "1.75")),
        scalp_steward_interval=float(os.getenv("SCALP_STEWARD_INTERVAL", "4")),
        scalp_fee_coverage_mult=float(os.getenv("SCALP_FEE_COVERAGE_MULT", "2.0")),
        scalp_3r_mode=_env_bool("SCALP_3R_MODE", True),
        scalp_3r_min_rr=float(os.getenv("SCALP_3R_MIN_RR", "3.0")),
        scalp_3r_harvest_min_r=float(os.getenv("SCALP_3R_HARVEST_MIN_R", "2.5")),
        scalp_3r_min_score_bump=float(os.getenv("SCALP_3R_MIN_SCORE_BUMP", "7")),
        scalp_3r_min_confidence_bump=float(os.getenv("SCALP_3R_MIN_CONFIDENCE_BUMP", "0.04")),
        winner_only_mode=_env_bool("WINNER_ONLY_MODE", True),
        winner_min_confluence=float(os.getenv("WINNER_MIN_CONFLUENCE", "0.62")),
        winner_min_agreeing=int(os.getenv("WINNER_MIN_AGREEING", "6")),
        winner_max_opposing=int(os.getenv("WINNER_MAX_OPPOSING", "2")),
        winner_min_ml_confidence=float(os.getenv("WINNER_MIN_ML_CONFIDENCE", "0.72")),
        winner_min_volume_ratio=float(os.getenv("WINNER_MIN_VOLUME_RATIO", "1.25")),
        winner_max_vwap_chase_pct=float(os.getenv("WINNER_MAX_VWAP_CHASE_PCT", "0.018")),
        winner_min_score=float(os.getenv("WINNER_MIN_SCORE", "0.58")),
        winner_elite_score=float(os.getenv("WINNER_ELITE_SCORE", "0.72")),
        winner_apex_score=float(os.getenv("WINNER_APEX_SCORE", "0.78")),
        winner_apex_preferred=_env_bool("WINNER_APEX_PREFERRED", True),
        winner_apex_starve_minutes=int(os.getenv("WINNER_APEX_STARVE_MINUTES", "45")),
        winner_elite_entry_gap_seconds=float(os.getenv("WINNER_ELITE_ENTRY_GAP_SECONDS", "22")),
        winner_elite_only=_env_bool("WINNER_ELITE_ONLY", True),
        winner_require_ml_align=_env_bool("WINNER_REQUIRE_ML_ALIGN", False),
        winner_min_ml_margin=float(os.getenv("WINNER_MIN_ML_MARGIN", "0.10")),
        winner_min_anchor_votes=int(os.getenv("WINNER_MIN_ANCHOR_VOTES", "2")),
        winner_max_opposition_ratio=float(os.getenv("WINNER_MAX_OPPOSITION_RATIO", "0.38")),
        winner_ml_veto_min_confidence=float(os.getenv("WINNER_ML_VETO_MIN_CONFIDENCE", "0.58")),
        margin_use_fraction=float(os.getenv("MARGIN_USE_FRACTION", "0.68")),
        min_margin_rate=float(os.getenv("MIN_MARGIN_RATE", "1.18")),
        target_margin_rate=float(os.getenv("TARGET_MARGIN_RATE", "1.30")),
        margin_top_up_enabled=_env_bool("MARGIN_TOP_UP_ENABLED", False),
        margin_mode=os.getenv("MARGIN_MODE", "cross").strip().lower(),
        auto_cross_margin_migrate=_env_bool("AUTO_CROSS_MARGIN_MIGRATE", True),
        auto_cross_margin_interval_sec=float(
            os.getenv("AUTO_CROSS_MARGIN_INTERVAL_SEC", "90")
        ),
        auto_cross_margin_symbol_cooldown_sec=float(
            os.getenv("AUTO_CROSS_MARGIN_SYMBOL_COOLDOWN_SEC", "300")
        ),
        max_effective_leverage=int(os.getenv("MAX_EFFECTIVE_LEVERAGE", "18")),
        max_stop_liq_fraction=float(os.getenv("MAX_STOP_LIQ_FRACTION", "0.22")),
        pre_liquidation_exit_factor=float(os.getenv("PRE_LIQUIDATION_EXIT_FACTOR", "0.55")),
        sl_liq_buffer=float(os.getenv("SL_LIQ_BUFFER", "0.50")),
        entries_paused=_env_bool("ENTRIES_PAUSED", False),
        max_opens_per_tick=int(os.getenv("MAX_OPENS_PER_TICK", "8")),
        small_account_max_open=int(os.getenv("SMALL_ACCOUNT_MAX_OPEN", "2")),
        small_account_max_opens_per_tick=int(os.getenv("SMALL_ACCOUNT_MAX_OPENS_PER_TICK", "1")),
        min_free_margin_pct=float(os.getenv("MIN_FREE_MARGIN_PCT", "0.18")),
        small_account_min_free_pct=float(os.getenv("SMALL_ACCOUNT_MIN_FREE_PCT", "0.28")),
        htf_required=_env_bool("HTF_REQUIRED", True),
        min_confluence_agreeing=int(os.getenv("MIN_CONFLUENCE_AGREEING", "2")),
        max_same_side_positions=int(os.getenv("MAX_SAME_SIDE_POSITIONS", "0")),
        require_ml_model=_env_bool("REQUIRE_ML_MODEL", True),
        max_funding_long=float(os.getenv("MAX_FUNDING_LONG", "0.0004")),
        min_funding_short=float(os.getenv("MIN_FUNDING_SHORT", "-0.0004")),
        optimizer_enabled=_env_bool("OPTIMIZER_ENABLED", True),
        optimizer_interval_seconds=float(os.getenv("OPTIMIZER_INTERVAL_SECONDS", "900")),
        optimizer_target_min_tph=int(os.getenv("OPTIMIZER_TARGET_MIN_TPH", "1")),
        optimizer_target_max_tph=int(os.getenv("OPTIMIZER_TARGET_MAX_TPH", "9999")),
        optimizer_quality_first=_env_bool("OPTIMIZER_QUALITY_FIRST", True),
        optimizer_flow_learning_enabled=_env_bool("OPTIMIZER_FLOW_LEARNING_ENABLED", True),
        optimizer_flow_target_tph=int(os.getenv("OPTIMIZER_FLOW_TARGET_TPH", "3")),
        optimizer_flow_lookback_reports=int(os.getenv("OPTIMIZER_FLOW_LOOKBACK_REPORTS", "80")),
        live_update_enabled=_env_bool("LIVE_UPDATE_ENABLED", True),
        live_update_poll_seconds=float(os.getenv("LIVE_UPDATE_POLL_SECONDS", "3")),
        live_update_git_pull=_env_bool("LIVE_UPDATE_GIT_PULL", False),
        live_update_git_interval_seconds=float(
            os.getenv("LIVE_UPDATE_GIT_INTERVAL_SECONDS", "300")
        ),
        throughput_brain_enabled=_env_bool("THROUGHPUT_BRAIN_ENABLED", True),
        leverage_rotate_on_start=_env_bool("LEVERAGE_ROTATE_ON_START", False),
        leverage_rotate_when_starved=_env_bool("LEVERAGE_ROTATE_WHEN_STARVED", False),
        leverage_rotate_interval_minutes=int(os.getenv("LEVERAGE_ROTATE_INTERVAL_MINUTES", "45")),
        leverage_auto_upgrade=_env_bool("LEVERAGE_AUTO_UPGRADE", False),
        stack_winners_mode=_env_bool("STACK_WINNERS_MODE", True),
        early_harvest_enabled=_env_bool("EARLY_HARVEST_ENABLED", False),
        moon_swarm_enabled=_env_bool("MOON_SWARM_ENABLED", True),
        markov_regime_enabled=_env_bool("MARKOV_REGIME_ENABLED", True),
        markov_confidence_boost_trend=float(os.getenv("MARKOV_CONFIDENCE_BOOST_TREND", "0.02")),
        markov_confidence_penalty_stress=float(os.getenv("MARKOV_CONFIDENCE_PENALTY_STRESS", "0.06")),
        symbol_quality_enabled=_env_bool("SYMBOL_QUALITY_ENABLED", True),
        symbol_quality_floor=float(os.getenv("SYMBOL_QUALITY_FLOOR", "0.22")),
        exec_slippage_penalty_enabled=_env_bool("EXEC_SLIPPAGE_PENALTY_ENABLED", True),
        exec_slippage_warn_bps=float(os.getenv("EXEC_SLIPPAGE_WARN_BPS", "12")),
        optimizer_autocode_enabled=_env_bool("OPTIMIZER_AUTOCODE_ENABLED", True),
        hourly_brain_enabled=_env_bool("HOURLY_BRAIN_ENABLED", True),
        hourly_autocode_enabled=_env_bool("HOURLY_AUTOCODE_ENABLED", True),
        trade_lessons_enabled=_env_bool("TRADE_LESSONS_ENABLED", True),
        trade_lesson_ml_refit_every=int(os.getenv("TRADE_LESSON_ML_REFIT_EVERY", "2")),
        optimizer_autocode_cooldown_seconds=int(os.getenv("OPTIMIZER_AUTOCODE_COOLDOWN_SECONDS", "900")),
        llm_trading_enabled=_env_bool("LLM_TRADING_ENABLED", True),
        llm_trading_min_confidence=float(os.getenv("LLM_TRADING_MIN_CONFIDENCE", "0.58")),
        llm_trading_temperature=float(os.getenv("LLM_TRADING_TEMPERATURE", "0.10")),
        llm_trading_max_tokens=int(os.getenv("LLM_TRADING_MAX_TOKENS", "128")),
        llm_trading_respect_markov=_env_bool("LLM_TRADING_RESPECT_MARKOV", True),
        llm_trading_fail_open=_env_bool("LLM_TRADING_FAIL_OPEN", True),
        llm_trading_min_score=float(os.getenv("LLM_TRADING_MIN_SCORE", "50")),
        llm_trading_use_cortex=_env_bool("LLM_TRADING_USE_CORTEX", True),
        llm_trading_strict=_env_bool("LLM_TRADING_STRICT", False),
        llm_only_trading=_env_bool("LLM_ONLY_TRADING", False),
        llm_policy_cache_sec=float(os.getenv("LLM_POLICY_CACHE_SEC", "45")),
        momentum_wave_mode=_env_bool("MOMENTUM_WAVE_MODE", True),
        momentum_wave_target_daily_pct=float(os.getenv("MOMENTUM_WAVE_TARGET_DAILY_PCT", "5.0")),
        momentum_wave_target_levered_profit_pct=float(
            os.getenv("MOMENTUM_WAVE_TARGET_LEVERED_PROFIT_PCT", "50.0")
        ),
        hourly_3r_winner_mode=_env_bool("HOURLY_3R_WINNER_MODE", True),
        optimizer_target_min_wins_per_hour=int(
            os.getenv("OPTIMIZER_TARGET_MIN_WINS_PER_HOUR", "3")
        ),
        tpsl_only_pacing=_env_bool("TPSL_ONLY_PACING", False),
        tpsl_pace_base_gap_seconds=float(os.getenv("TPSL_PACE_BASE_GAP_SECONDS", "2")),
        tpsl_pace_gap_after_tp_seconds=float(os.getenv("TPSL_PACE_GAP_AFTER_TP_SECONDS", "4")),
        tpsl_pace_gap_after_sl_seconds=float(os.getenv("TPSL_PACE_GAP_AFTER_SL_SECONDS", "12")),
        tpsl_pace_symbol_sl_cooldown_seconds=float(
            os.getenv("TPSL_PACE_SYMBOL_SL_COOLDOWN_SECONDS", "25")
        ),
        scalp_fast_3r=_env_bool("SCALP_FAST_3R", True),
        scalp_fast_max_stop_pct=float(os.getenv("SCALP_FAST_MAX_STOP_PCT", "0.010")),
        scalp_fast_max_take_pct=float(os.getenv("SCALP_FAST_MAX_TAKE_PCT", "0.030")),
        scalp_momentum_max_stop_pct=float(os.getenv("SCALP_MOMENTUM_MAX_STOP_PCT", "0.014")),
        scalp_skip_liq_tpsl=_env_bool("SCALP_SKIP_LIQ_TPSL", True),
        scalp_roe_harvest_enabled=_env_bool("SCALP_ROE_HARVEST_ENABLED", False),
        scalp_roe_harvest_min_pct=float(os.getenv("SCALP_ROE_HARVEST_MIN_PCT", "50")),
        scalp_roe_harvest_max_pct=float(os.getenv("SCALP_ROE_HARVEST_MAX_PCT", "60")),
    )