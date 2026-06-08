"""Strategy specifications — shared TA confluence; tiers differ by gates + sizing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BotSpec:
    id: str
    name: str
    description: str
    min_confluence: float
    min_agreeing: int
    min_composite_score: float
    min_confidence: float
    runner_filter: bool
    require_runner: bool
    skip_choppy: bool
    min_runner_score: float
    max_chop: float
    min_path_eff: float
    three_r_mode: bool
    max_stop_pct: float
    max_take_pct: float
    min_rr: float
    atr_stop_mult: float
    atr_take_mult: float
    risk_per_trade: float
    entry_gap_bars: int
    fee_roundtrip_pct: float = 0.05
    require_htf_align: bool = False
    trend_with_period: bool = False
    trend_only: bool = False
    min_adx_1h: float = 8.0
    pullback_band: float = 0.004


# Direction follows ta_confluence long/short — no macro period override.

FAST_LANE = BotSpec(
    id="god-bot-3r-fast",
    name="Bob's 3R Fast Lane",
    description="TA confluence 3R; loosest gates, highest size",
    min_confluence=0.47,
    min_agreeing=4,
    min_composite_score=46.0,
    min_confidence=0.54,
    runner_filter=True,
    require_runner=False,
    skip_choppy=False,
    min_runner_score=0.34,
    max_chop=0.65,
    min_path_eff=0.16,
    three_r_mode=True,
    max_stop_pct=0.010,
    max_take_pct=0.030,
    min_rr=3.0,
    atr_stop_mult=1.08,
    atr_take_mult=2.0,
    risk_per_trade=0.028,
    entry_gap_bars=5,
    min_adx_1h=7.0,
    pullback_band=0.006,
)

SCALPER_PRO = BotSpec(
    id="god-bot-scalper-pro",
    name="Bob's Scalper Pro",
    description="TA confluence 3R; balanced throughput",
    min_confluence=0.48,
    min_agreeing=4,
    min_composite_score=48.0,
    min_confidence=0.56,
    runner_filter=True,
    require_runner=False,
    skip_choppy=False,
    min_runner_score=0.36,
    max_chop=0.62,
    min_path_eff=0.18,
    three_r_mode=True,
    max_stop_pct=0.010,
    max_take_pct=0.030,
    min_rr=3.0,
    atr_stop_mult=1.10,
    atr_take_mult=2.0,
    risk_per_trade=0.026,
    entry_gap_bars=6,
    min_adx_1h=8.0,
    pullback_band=0.005,
)

GOD_BOT = BotSpec(
    id="god-bot",
    name="God Bot (live)",
    description="TA confluence 3R flagship; live relaxes gates when throughput starved",
    min_confluence=0.48,
    min_agreeing=4,
    min_composite_score=49.0,
    min_confidence=0.57,
    runner_filter=True,
    require_runner=False,
    skip_choppy=False,
    min_runner_score=0.38,
    max_chop=0.60,
    min_path_eff=0.20,
    three_r_mode=True,
    max_stop_pct=0.010,
    max_take_pct=0.030,
    min_rr=3.0,
    atr_stop_mult=1.12,
    atr_take_mult=2.05,
    risk_per_trade=0.025,
    entry_gap_bars=6,
    min_adx_1h=8.0,
    pullback_band=0.004,
)

ML_CORTEX = BotSpec(
    id="god-bot-ml-cortex",
    name="Bob's ML Cortex",
    description="TA confluence 3R quality tier; tighter scores, conservative size",
    min_confluence=0.49,
    min_agreeing=4,
    min_composite_score=50.0,
    min_confidence=0.58,
    runner_filter=True,
    require_runner=False,
    skip_choppy=False,
    min_runner_score=0.40,
    max_chop=0.58,
    min_path_eff=0.22,
    three_r_mode=True,
    max_stop_pct=0.010,
    max_take_pct=0.030,
    min_rr=3.0,
    atr_stop_mult=1.14,
    atr_take_mult=2.1,
    risk_per_trade=0.023,
    entry_gap_bars=7,
    min_adx_1h=9.0,
    pullback_band=0.003,
)

BOT_SPECS: dict[str, BotSpec] = {s.id: s for s in (GOD_BOT, SCALPER_PRO, FAST_LANE, ML_CORTEX)}


def get_base_spec(bot_id: str) -> BotSpec:
    key = bot_id.strip().lower()
    aliases = {
        "god-bot-scalper-pro": "god-bot-scalper-pro",
        "god-bot-3r-fast": "god-bot-3r-fast",
        "god-bot-ml-cortex": "god-bot-ml-cortex",
        "god-bot": "god-bot",
    }
    rid = aliases.get(key, key)
    return BOT_SPECS.get(rid, SCALPER_PRO)


def get_spec(bot_id: str) -> BotSpec:
    from bobs_bots.tune import get_spec as tuned

    return tuned(bot_id)
