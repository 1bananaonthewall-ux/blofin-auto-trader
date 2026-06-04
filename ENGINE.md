# Fluid Autonomous Engine

**Mission:** **Maintain and exceed 10% account growth per day** (`mission_config.py`) — the engine has **one purpose only** (`mission_brain.py`). Every subsystem asks whether an action maintains or exceeds today's +10% path. Nothing else is optimized.

There are no labeled modes ("recovery", "chill", etc.). Like a skilled trader, the bot **continuously** adjusts intensity — it does not announce a state change.

## Architecture

```
Market data → ML ensemble (4 models, 28+ features)
                    ↓
            Fluid manifold (~35 continuous factors, learnable weights)
                    ↓
         path_reliability × action_intensity
                    ↓
         Runtime knobs (confidence, risk, scan, leverage)
                    ↓
         Margin-gated execution (SL/TP always, no flip-exits)
         One highest-conviction open per cycle (entry pacer)
```

### Live market hub (WebSocket + REST)

- **REST** refreshes **all** tickers every ~12s (instant full-universe prices)
- **WebSocket** streams tickers + 1m/5m candles for priority symbols (open positions + top movers)
- ML scan prioritizes highest-momentum symbols from the live ticker field

### Position steward (`position_steward.py`)

- **Background loop** (every ~5–12s when positions are open) manages **every** live trade:
  - Adopts positions opened manually or before restart into the registry
  - Re-checks **SL/TP** on all positions
  - **Harvests** fee-matured winners
  - Keeps **stream priority + candles** on all open symbols
- Main scan cycle also runs a steward pass before new entries so management never waits on a slow scan.

### Position rotation

- **Harvest**: close winners that have **matured past fees** (not signal-flip)
- **Upgrade**: if margin is tight, rotate out the weakest open slot for a much higher-conviction setup
- **Cycle in**: best conviction(s) per paced cycle, margin-sized from live free balance

### TA confluence (`ta_confluence.py`)

Every scan runs **15+ independent analyses** (EMA 1m/5m, RSI, MACD, Bollinger, VWAP, HTF, ADX, MFI, CMF, structure, volume, funding, ML).

- Builds a **confluence zone** — only methods that agree on direction count
- Requires **≥52% weighted agreement** and **≥5 agreeing votes**
- Rejects setups where opposing signals outnumber agreeing ones
- Ranks **highest conviction** = confluence × composite score × path reliability

### Conviction execution (not machine-gunning)

- **Adaptive scan** (`scan_orchestrator.py`): each tick analyzes a dynamic slice of **all** exchange assets (not a fixed 60). Depth scales with fluid intensity, PnL curve, ticker freshness, and open load — high conviction periods sweep the full universe in ~5–8 ticks; flat/declining curves throttle back.
- Scans many symbols each tick to **find** the best setups
- Opens **#1**, plus **only** others **damn-near tied** (≤0.022 abs or ≤3.5% rel gap from top), max **3** per cycle
- Waits **~75–120s** before the next cycle’s entries (ties in the same cycle open together)
- Margin split across tied elites (~92% of per-slot budget each)

### Mission brain (`mission_brain.py`)

- **Sole objective:** Maintain and exceed 10% account growth per day — no secondary goals
- Each tick: schedule pressure, mission focus score, risk multiplier, conviction floor
- **Vetoes** entries that do not serve the path (below +10% → elite setups only; declining curve → protect base)
- **Amplifies** risk when below +10% *and* the PnL curve is climbing/vertical with strong conviction
- Hourly log prints the internal directive (e.g. `BELOW +10% — press high-conviction trades to maintain/exceed daily goal`)

### PnL curve (`pnl_curve.py`)

The engine **knows its equity curve** from `state/equity_ticks.jsonl` and closed trades in `state/profitability.json`.

Each tick it measures:

| Metric | Role |
|--------|------|
| `verticality` | How steep/upward the curve is (0–1) |
| `curve_phase` | `vertical` / `climbing` / `flat` / `declining` |
| `harvest_eagerness` | Bank fee-matured winners sooner when curve flattens |
| `entry_scale` / `risk_scale` | Size and press entries only when curve supports it |

**Goal:** do everything possible to keep the curve **vertical** — tighten entries when flat, harvest aggressively when not climbing, protect capital on decline. Hourly logs include a verticality bar.

### Fluid manifold (`fluid_manifold.py`)

Each tick computes factors in `[0, 1]`, including:

- Equity health, drawdown depth, recovery slope, **PnL verticality / curve slope / acceleration**
- Velocity (5m / 15m / 30m drops and rises)
- Win rate, profit factor, loss streak
- Schedule pressure vs $95M path
- ML accuracy and long/short edge
- Margin headroom, position load, feedback depth

They blend into:

| Output | Meaning |
|--------|---------|
| `path_reliability` | Safe to act toward target now? |
| `action_intensity` | How hard to scan / size / press |
| `survival` | Account protection pressure |
| `edge` | Recent + model edge quality |

New entries only when **both** reliability and intensity justify it — smoothly, not on/off modes.

Weights in `state/manifold_weights.json` nudge over time from live outcomes.

### ML ensemble (`ml/trainer.py`)

- **HistGradientBoosting** + **RandomForest** + **LogisticRegression** + **ExtraTrees**
- **30 features** (returns, RSI, MACD, BB, ADX, MFI, wicks, HTF, etc.)
- **Training universe**: every live USDT perp when `TRADE_UNIVERSE=all` (`ML_TRAIN_SYMBOLS=0`)
- **Continuous trainer** (`ml/universe_trainer.py`): background loop cycles all exchange assets, accumulates shards, refits after each full pass
- Walk-forward validation + live `trade_outcomes.jsonl` feedback on retrain

### Doctrine (never broken)

- SL/TP on every entry; maintained on open positions
- No bot-side signal-flip closes
- Unlimited positions, **margin is the limit**
- Retrain on schedule + when manifold detects deteriorating edge

## Commands

```powershell
cd C:\Users\mknig\blofin-auto-trader
python status.py
python bot.py
```

## On "quadrillion parameters"

A literal quadrillion weights is not meaningful. What you have is a **high-dimensional control field**: thousands of ML tree splits plus dozens of interacting manifold factors with learnable weights — all serving one objective: **reliable progress to $95M**, not random aggression.
