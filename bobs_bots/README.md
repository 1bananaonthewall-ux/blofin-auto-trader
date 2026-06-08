# Bob's Bots — real strategy profiles

Three product bots + **God Bot** share the same TA core (`ta_confluence.py`). They differ by **confluence thresholds, runner filter, and pacing** — not different indicator stacks.

| Bot | ID | Gates |
|-----|-----|-------|
| God Bot (live) | `god-bot` | Adaptive: strict (0.54/6 votes, runner required) unless 3R throughput starved |
| Bob's Scalper Pro | `god-bot-scalper-pro` | Strict + balanced risk |
| Bob's 3R Fast Lane | `god-bot-3r-fast` | Loose (0.48/4), more trades |
| Bob's ML Cortex | `god-bot-ml-cortex` | Elite runner-only, highest gates |

## Backtest engine

- Loads Blofin **5m + 1H** OHLCV for the date range
- Walks bars; calls `run_all_analyses` + runner filter per bot spec
- Simulates **3R bracket** exits on bar high/low (fees included)

```powershell
python scripts\compare_bots_backtest.py --days 90 --symbols BTC-USDT,ETH-USDT --pot 1000
```

Storefront **Backtest Lab** uses the same engine via `storefront_backtest.py`.
