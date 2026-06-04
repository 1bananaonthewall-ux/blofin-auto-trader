"""
Markov regime filter — MIT/Hamilton-style latent states (free, local).

Three hidden states with a transition matrix P(S_t | S_{t-1}) and
emission likelihoods from returns, ATR%, and ADX. Uses forward filtering
(one-step belief update), not paid LLMs.

States:
  calm   — low vol, mean-reverting friendly
  trend  — directional, momentum friendly
  stress — high vol / shock, tighten entries and widen effective risk control
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from indicators import adx, atr

log = logging.getLogger(__name__)

STATE_NAMES = ("calm", "trend", "stress")
N_STATES = 3

# Persistent regimes (Hamilton 1989): high diagonal = regime persistence
DEFAULT_TRANSITION = [
    [0.86, 0.11, 0.03],
    [0.09, 0.82, 0.09],
    [0.12, 0.18, 0.70],
]


@dataclass(frozen=True)
class MarkovSnapshot:
    state: str
    state_id: int
    probs: tuple[float, float, float]
    persistence: float
    transition_risk: float
    entry_scale: float
    stop_mult: float
    harvest_mult: float
    summary: str


def _softmax3(a: float, b: float, c: float) -> tuple[float, float, float]:
    m = max(a, b, c)
    ea, eb, ec = math.exp(a - m), math.exp(b - m), math.exp(c - m)
    s = ea + eb + ec
    return ea / s, eb / s, ec / s


def _observation_features(ohlcv_1m: list[list[float]]) -> tuple[float, float, float] | None:
    if len(ohlcv_1m) < 25:
        return None
    closes = [float(r[4]) for r in ohlcv_1m]
    if closes[-2] <= 0:
        return None
    ret = (closes[-1] - closes[-2]) / closes[-2]
    atr_val = atr(ohlcv_1m, 14) or 0.0
    atr_pct = atr_val / closes[-1] if closes[-1] > 0 else 0.0
    adx_val = adx(ohlcv_1m, 14) or 0.0
    return ret, atr_pct, adx_val


def emission_likelihoods(ret: float, atr_pct: float, adx_val: float) -> tuple[float, float, float]:
    """Log-scale Gaussian bumps per state (emission model)."""
    ar = abs(ret)
    calm = -((ar / 0.0012) ** 2) - ((max(0.0, atr_pct - 0.012) / 0.008) ** 2)
    trend = -((max(0.0, 22.0 - adx_val) / 12.0) ** 2) - ((ar / 0.004) ** 2) * 0.35
    stress = -((max(0.0, atr_pct - 0.022) / 0.012) ** 2) - ((ar / 0.006) ** 2)
    return _softmax3(calm, trend, stress)


def forward_filter(
    prior: tuple[float, float, float],
    emissions: tuple[float, float, float],
    transition: list[list[float]],
) -> tuple[float, float, float]:
    """b_t(j) ∝ emission_j * sum_i b_{t-1}(i) * T_ij"""
    out = [0.0, 0.0, 0.0]
    for j in range(N_STATES):
        for i in range(N_STATES):
            out[j] += prior[i] * transition[i][j] * emissions[j]
    return _softmax3(out[0], out[1], out[2])


def snapshot_from_probs(
    probs: tuple[float, float, float], transition: list[list[float]] | None = None
) -> MarkovSnapshot:
    transition = transition or DEFAULT_TRANSITION
    sid = max(range(N_STATES), key=lambda i: probs[i])
    state = STATE_NAMES[sid]
    t_row = transition[sid]
    persistence = t_row[sid]
    transition_risk = t_row[2] if sid != 2 else t_row[2]

    if state == "calm":
        entry_scale, stop_mult, harvest_mult = 1.0, 1.0, 1.0
    elif state == "trend":
        entry_scale, stop_mult, harvest_mult = 1.06, 0.95, 0.92
    else:
        entry_scale, stop_mult, harvest_mult = 0.82, 1.25, 1.15

    if transition_risk > 0.12:
        entry_scale *= max(0.75, 1.0 - transition_risk * 1.5)

    summary = (
        f"markov {state} p={probs[sid]:.0%} persist={persistence:.0%} "
        f"stress_next={transition_risk:.0%}"
    )
    return MarkovSnapshot(
        state=state,
        state_id=sid,
        probs=probs,
        persistence=round(persistence, 4),
        transition_risk=round(transition_risk, 4),
        entry_scale=round(entry_scale, 4),
        stop_mult=round(stop_mult, 4),
        harvest_mult=round(harvest_mult, 4),
        summary=summary,
    )


class MarkovRegimeEngine:
    """Per-key forward filters (symbol or 'global')."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._beliefs: dict[str, tuple[float, float, float]] = {}
        self._last: dict[str, MarkovSnapshot] = {}
        self._transition = [row[:] for row in DEFAULT_TRANSITION]
        self._counts = [[1.0 for _ in range(N_STATES)] for _ in range(N_STATES)]
        self._state_dir = state_dir
        self._last_save_ts = 0.0
        if state_dir:
            self._load(state_dir)

    def _load(self, state_dir: Path) -> None:
        p = state_dir / "markov_regime.json"
        if not p.is_file():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            t = raw.get("transition")
            if isinstance(t, list) and len(t) == N_STATES:
                self._transition = t
            c = raw.get("counts")
            if isinstance(c, list) and len(c) == N_STATES:
                self._counts = c
        except Exception as exc:
            log.debug("markov load: %s", exc)

    def save(self, force: bool = False) -> None:
        if not self._state_dir:
            return
        now = time.time()
        if not force and (now - self._last_save_ts) < 30.0:
            return
        p = self._state_dir / "markov_regime.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"transition": self._transition, "counts": self._counts, "ts": time.time()},
                indent=2,
            ),
            encoding="utf-8",
        )
        self._last_save_ts = now

    def _renormalize(self) -> None:
        for i in range(N_STATES):
            row_sum = sum(float(v) for v in self._counts[i]) or 1.0
            self._transition[i] = [max(1e-4, float(v) / row_sum) for v in self._counts[i]]

    def observe_transition(self, from_state: int, to_state: int, reward: float = 0.0) -> None:
        if from_state < 0 or from_state >= N_STATES or to_state < 0 or to_state >= N_STATES:
            return
        bump = 1.0 + max(-0.3, min(0.3, reward)) * 0.4
        self._counts[from_state][to_state] = float(self._counts[from_state][to_state]) + max(0.25, bump)
        self._renormalize()
        self.save()

    def observe_outcome(self, state: str, win: bool, stress_p: float = 0.0) -> None:
        if state not in STATE_NAMES:
            return
        sid = STATE_NAMES.index(state)
        if win:
            self.observe_transition(sid, sid, reward=0.15)
            if sid == 2 and stress_p < 0.5:
                self.observe_transition(2, 1, reward=0.1)
        else:
            self.observe_transition(sid, 2, reward=-0.15)
            if sid == 0:
                self.observe_transition(0, 1, reward=-0.08)
        self.save(force=True)

    def update(self, key: str, ohlcv_1m: list[list[float]]) -> MarkovSnapshot | None:
        feats = _observation_features(ohlcv_1m)
        if feats is None:
            return self._last.get(key)
        ret, atr_pct, adx_val = feats
        prior = self._beliefs.get(key, (1.0 / 3, 1.0 / 3, 1.0 / 3))
        emit = emission_likelihoods(ret, atr_pct, adx_val)
        post = forward_filter(prior, emit, self._transition)
        prev_sid = max(range(N_STATES), key=lambda i: prior[i])
        sid = max(range(N_STATES), key=lambda i: post[i])
        if sid != prev_sid:
            self.observe_transition(prev_sid, sid)
        self._beliefs[key] = post
        snap = snapshot_from_probs(post, transition=self._transition)
        self._last[key] = snap
        return snap

    def last(self, key: str = "global") -> MarkovSnapshot | None:
        return self._last.get(key)


_engine: MarkovRegimeEngine | None = None


def get_markov_engine(state_dir: Path | None = None) -> MarkovRegimeEngine:
    global _engine
    if _engine is None:
        _engine = MarkovRegimeEngine(state_dir=state_dir)
    return _engine
