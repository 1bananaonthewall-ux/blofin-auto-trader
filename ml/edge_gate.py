"""Rolling live edge from closed trades — tightens entry when expectancy is negative."""

from __future__ import annotations

import json
from pathlib import Path


def rolling_expectancy(
    state_dir: Path,
    *,
    window: int = 24,
) -> tuple[float | None, int]:
    """
    Mean signed R from recent outcomes (win=+1R, loss=-1R proxy).
    Returns (expectancy, n) or (None, 0) if insufficient data.
    """
    path = state_dir / "trade_outcomes.jsonl"
    if not path.exists():
        return None, 0
    rs: list[float] = []
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()[-800:]):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "outcome":
                continue
            r = row.get("r_multiple")
            if r is not None:
                rs.append(float(r))
            elif row.get("outcome") == "win" or int(row.get("win", 0)) == 1:
                rs.append(1.0)
            else:
                rs.append(-1.0)
            if len(rs) >= window:
                break
    except Exception:
        return None, 0
    if len(rs) < 8:
        return None, len(rs)
    return sum(rs) / len(rs), len(rs)
