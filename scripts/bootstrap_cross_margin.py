#!/usr/bin/env python3
"""Apply cross margin env, sync exchange, rotate ML shards, retrain on cross outcomes."""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from apply_cross_margin_env import main as apply_env

    apply_env()

    from config import load_settings
    from exchange_client import BlofinExchange
    from margin_mode import normalize_margin_mode

    settings = load_settings()
    want = normalize_margin_mode(settings.margin_mode)
    print(f"margin_mode={want}")

    ex = BlofinExchange(settings)
    ex.load()
    if settings.mode == "live" and not settings.dry_run:
        ex.ensure_account_margin_mode()
    else:
        print("dry_run or non-live — skipped exchange margin-mode API")

    shard_dir = settings.state_dir / "ml_shards"
    archive = settings.state_dir / f"ml_shards_isolated_{int(time.time())}"
    if shard_dir.exists() and any(shard_dir.glob("*.npz")):
        shutil.move(str(shard_dir), str(archive))
        print(f"archived isolated shards -> {archive.name}")
    shard_dir.mkdir(parents=True, exist_ok=True)

    meta = settings.state_dir / "signal_model_meta.json"
    model = settings.state_dir / "signal_model.joblib"
    for path in (meta, model):
        if path.exists():
            bak = path.with_suffix(path.suffix + ".isolated.bak")
            if not bak.exists():
                shutil.copy2(path, bak)
            print(f"backed up {path.name}")

    print("training signal model on cross-margin profile...")
    rc = subprocess.call([sys.executable, str(ROOT / "train_model.py")], cwd=str(ROOT))
    if rc != 0:
        print(f"train_model exited {rc}")
        return rc

    print("done — restart bot: scripts/stack_control.ps1 -Action restart-fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
