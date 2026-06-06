#!/usr/bin/env python3
"""Train/retrain the hourly maintenance policy from labelled runs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings
from hourly_brain import label_previous_runs, maybe_train_policy


def main() -> int:
    settings = load_settings()
    state_dir = settings.state_dir
    snap_path = state_dir / "hourly_report.json"
    equity = 0.0
    opens = 0
    if snap_path.is_file():
        import json

        rep = json.loads(snap_path.read_text(encoding="utf-8"))
        equity = float(rep.get("equity") or 0)
        opens = int((rep.get("tuning") or {}).get("trades_last_hour", 0) or 0)
    label_previous_runs(state_dir, equity, opens)
    ok = maybe_train_policy(state_dir)
    print("trained" if ok else "need_more_samples (24+ labelled action rows)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
