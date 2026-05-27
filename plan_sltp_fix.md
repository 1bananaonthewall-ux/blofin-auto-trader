# SL/TP Fix Plan

## Root Causes Identified

1. **SL/TP placed AFTER market order in two steps** (exchange_client.py lines 242-363):
   - Step 1: Market order entry (no SL/TP attached)
   - Step 4: SL/TP via separate `place_algo_order` call
   - If the algo order placement fails (API error, rate limit, exchange rejects), the position has NO SL/TP

2. **No retry/verification that SL/TP actually exists**:
   - The code assumes step 4 succeeded and doesn't verify
   - Next tick's `amend_position_sltp()` tries `amend-algo` first, which fails if no algo exists, then falls back to `place_algo_order` - but if this also fails, still no SL/TP

3. **No pre-liquidation detection**:
   - The code only widens the stop to be outside liquidation distance at entry time
   - No ongoing check to detect if price is approaching liquidation and exit proactively

4. **Existing position loop depends on `analyze_symbol`**:
   - In `run_once()` line 389-422, existing positions are processed through `analyze_symbol`+`manage_symbol`
   - If `analyze_symbol` returns None, a fallback decision with hardcoded 1% stop/2% take is used
   - But the SL/TP amendment only runs if `settings.update_existing_sltp` is True (which it is in .env)

## Fixes

### 1. Add `ensure_sltp()` method to exchange_client.py
- Takes position data, forces SL/TP algo order placement
- Retries on failure with backoff
- Verifies SL/TP was placed by checking algo order list

### 2. Add pre-liquidation detection to bot.py
- Check distance from current price to liquidation level
- Exit proactively if price approaches liquidation

### 3. Make existing position loop always ensure SL/TP
- Separate SL/TP enforcement from signal analysis
- Every tick, for every open position, ensure SL/TP is set

### 4. Improve SL/TP placement in initial order
- Include `tpTriggerPrice` and `slTriggerPrice` in initial market order body as PRIMARY method
- Keep algo order placement as fallback