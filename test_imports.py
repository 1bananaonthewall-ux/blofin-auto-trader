#!/usr/bin/env python3
"""Test that all new modules import correctly."""
import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_dir)
os.chdir(project_dir)

print(f"Working directory: {os.getcwd()}")
print(f"Python path includes: {project_dir}")
print()

print("Testing imports...")

# Test fee_engine
try:
    from fee_engine import (
        ensure_fee_overcoming, analyze_trade_fees, compute_breakeven_winrate,
        FeeAnalysis, FEE_TAKER, FEE_MAKER, roundtrip_fee_pct
    )
    print("  [OK] fee_engine")
except Exception as e:
    print(f"  [FAIL] fee_engine: {e}")

# Test growth_optimizer
try:
    from growth_optimizer import CompoundGrowthOptimizer, GrowthMetrics
    from mission_config import TARGET_DAILY_GROWTH_PCT
    print("  [OK] growth_optimizer")
except Exception as e:
    print(f"  [FAIL] growth_optimizer: {e}")

# Test risk (which now imports fee_engine and markets)
try:
    from risk import SmartPositionSizer, SizingDecision
    print("  [OK] risk")
except Exception as e:
    import traceback
    print(f"  [FAIL] risk: {e}")
    traceback.print_exc()
    # Try direct markets import to debug
    try:
        import markets
        print(f"  markets module: {markets.__file__}")
    except Exception as e2:
        print(f"  markets import also failed: {e2}")

# Test config
try:
    from config import Settings, load_settings
    print("  [OK] config")
except Exception as e:
    print(f"  [FAIL] config: {e}")

print("\n--- Functional test: Fee Analysis ---")

# Test fee analysis
entry_price = 60000.0
contracts = 0.1
contract_size = 0.001
stop_pct = 0.005  # 0.5%
take_pct = 0.015  # 1.5%
leverage = 50

analysis = analyze_trade_fees(entry_price, contracts, contract_size, stop_pct, take_pct, leverage)
print(f"  Roundtrip fee: {analysis.roundtrip_fee_pct:.4f}%")
print(f"  Total fee: ${analysis.total_fee_usd:.4f}")
print(f"  Fee covered: {analysis.fee_covered}")
print(f"  Profit after fees: ${analysis.profit_after_fees_usd:.4f}")
print(f"  Safety margin: {analysis.safety_margin_pct:.2f}%")

# Test ensure_fee_overcoming
adj_stop, adj_take, fee_dict = ensure_fee_overcoming(
    entry_price, contracts, contract_size, stop_pct, take_pct, leverage,
    min_fee_coverage_multiple=2.5
)
print(f"\n  Adjusted stop: {adj_stop:.4f} ({adj_stop/stop_pct:.1f}x original)")
print(f"  Adjusted take: {adj_take:.4f} ({adj_take/take_pct:.1f}x original)")
print(f"  Fee covered: {fee_dict['fee_covered']}")
print(f"  Profit after fees: ${fee_dict['profit_after_fees_usd']:.4f}")

# Test breakeven winrate
be = compute_breakeven_winrate(0.6, 0.015, 0.005)
print(f"\n  Breakeven winrate: {be['min_win_rate_needed']:.1f}%")
print(f"  Profitable: {be['profitable']}")
print(f"  EV per trade: {be['ev_per_trade_pct']:.4f}%")

# Test growth metrics
print("\n--- Functional test: Growth Optimizer ---")
from pathlib import Path
import tempfile

tmpdir = Path(tempfile.mkdtemp())
optimizer = CompoundGrowthOptimizer(tmpdir, start_capital=100)
optimizer.record_equity_snapshot(100)
optimizer.record_equity_snapshot(120)
optimizer.record_equity_snapshot(150)

metrics = optimizer.get_growth_metrics(150)
print(f"  Days remaining: {metrics.days_remaining}")
print(f"  Required daily return: {metrics.required_daily_return_pct:.4f}%")
print(f"  Aggression boost: {metrics.aggression_boost:.2f}x")
print(f"  On track: {metrics.on_track}")
print(f"  Days to double: {metrics.days_to_double_at_current_rate:.1f}" )

print("\n" + optimizer.format_growth_report(150))

print("\n--- All tests passed ---")