"""Quick test for 10x30 strategy imports."""
import sys
sys.path.insert(0, '.')
from strategy_10x30 import evaluate_10x30, StrategyDecision10x30, Signal
print("strategy_10x30 OK")
from signals import analyze_symbol
print("signals OK")
print("ALL IMPORTS OK")