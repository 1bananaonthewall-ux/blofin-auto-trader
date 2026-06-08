"""Bob's Bots — three real strategy profiles + God Bot mirror for backtesting."""

from bobs_bots.specs import BOT_SPECS, BotSpec, get_spec
from bobs_bots.simulator import backtest_symbol, compare_bots

__all__ = ["BOT_SPECS", "BotSpec", "get_spec", "backtest_symbol", "compare_bots"]
