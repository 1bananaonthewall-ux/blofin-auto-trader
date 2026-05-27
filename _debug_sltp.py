#!/usr/bin/env python3
"""Debug script to check config values."""
import os
os.chdir(r"C:\Users\mknig\blofin-auto-trader")
from config import load_settings
s = load_settings()
print(f"UPDATE_EXISTING_SLTP from .env: {os.getenv('UPDATE_EXISTING_SLTP')}")
print(f"settings.update_existing_sltp: {s.update_existing_sltp}")
print(f"settings.take_profit_pct: {s.take_profit_pct}")
print(f"settings.stop_loss_pct: {s.stop_loss_pct}")
print(f"settings.liquidation_buffer_pct: {s.liquidation_buffer_pct}")
print(f"settings.leverage: {s.leverage}")
print(f"settings.symbols_per_tick: {s.symbols_per_tick}")
print(f"settings.poll_seconds: {s.poll_seconds}")