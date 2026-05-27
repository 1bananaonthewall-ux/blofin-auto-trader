# AI / ML signal layer

This bot includes a **local learning model** that is separate from BloHunter’s cloud AI.

## What it is

- **Gradient boosting classifier** (`HistGradientBoostingClassifier` from scikit-learn)
- Trained on **your Blofin historical candles** (1m + 5m features)
- Labels: next 5 bars return ≥ +0.15% → long, ≤ −0.15% → short (neutral bars skipped)
- **11 features**: returns, RSI, EMA spread, ATR%, volume, higher-timeframe trend, funding, time-of-day
- Only **deployed** if validation accuracy and precision pass minimum gates (otherwise rules fallback)

## What it is not

- Not BloHunter’s proprietary model (we do not have their training data or signals)
- Not a guarantee of profit — crypto short-horizon direction is noisy
- Not “1000× better” by default — it improves as you collect more history and retrain

## Commands

```powershell
cd C:\Users\mknig\blofin-auto-trader
.\.venv\Scripts\pip install -r requirements.txt

# Train / retrain manually
.\.venv\Scripts\python.exe train_model.py

# Check model + account
.\.venv\Scripts\python.exe status.py
```

The live bot (`bot.py`) with `SIGNAL_MODE=ml` will:

1. Train on first start if no model exists
2. Retrain every `ML_RETRAIN_HOURS` (default 24)
3. Fall back to the enhanced rule strategy if the model fails quality checks

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `SIGNAL_MODE` | `ml` | `ml`, `enhanced`, or `rules` |
| `ML_MIN_CONFIDENCE` | `0.58` | Min probability to open long/short |
| `ML_TRAIN_SYMBOLS` | `40` | How many symbols to include per training run |
| `ML_HISTORY_BARS` | `300` | 1m candles per symbol for training |
| `ML_RETRAIN_HOURS` | `24` | Auto-retrain interval |

Model files: `state/signal_model.joblib`, `state/signal_model_meta.json`
