"""Load/save per-bot tuning overlays from auto-tune runs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from bobs_bots.specs import BotSpec, get_base_spec

ROOT = Path(__file__).resolve().parent.parent
TUNE_PATH = ROOT / "state" / "storefront" / "bot_tune_overrides.json"


def load_overrides() -> dict[str, dict[str, Any]]:
    if not TUNE_PATH.is_file():
        return {}
    try:
        return json.loads(TUNE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_overrides(data: dict[str, dict[str, Any]]) -> None:
    TUNE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TUNE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_spec(bot_id: str) -> BotSpec:
    spec = get_base_spec(bot_id)
    ov = load_overrides().get(spec.id, {})
    if not ov:
        return spec
    allowed = {f.name for f in spec.__dataclass_fields__.values()}
    kw = {k: v for k, v in ov.items() if k in allowed}
    return replace(spec, **kw) if kw else spec


def apply_tune_step(bot_id: str, *, score_delta: float = -1.5, conf_delta: float = -0.015) -> BotSpec:
    """Tighten quality and spacing for bots that still lose on some assets."""
    data = load_overrides()
    spec = get_spec(bot_id)
    row = data.get(bot_id, {})

    row["min_composite_score"] = float(row.get("min_composite_score", spec.min_composite_score)) + score_delta
    row["min_confidence"] = max(0.50, float(row.get("min_confidence", spec.min_confidence)) + conf_delta)
    row["min_confluence"] = max(0.45, float(row.get("min_confluence", spec.min_confluence)) - 0.005)
    row["entry_gap_bars"] = min(12, int(row.get("entry_gap_bars", spec.entry_gap_bars)) + 1)
    row["pullback_band"] = max(0.002, float(row.get("pullback_band", spec.pullback_band)) - 0.0005)
    row["risk_per_trade"] = max(0.018, float(row.get("risk_per_trade", spec.risk_per_trade)) - 0.001)
    row["require_runner"] = False
    row["skip_choppy"] = False

    data[bot_id] = row
    save_overrides(data)
    return get_spec(bot_id)
