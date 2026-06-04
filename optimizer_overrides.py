from __future__ import annotations


def apply_overrides(
    conf_gate: float,
    score_gate: float,
    *,
    markov_state: str = "",
    trades_last_hour: int = 0,
):
    # Neutral default. optimizer_autocode may rewrite this file.
    return conf_gate, score_gate
