"""Shared kwargs for build_training_matrix from Settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings


def training_matrix_kwargs(settings: "Settings") -> dict:
    return {
        "forward_bars": settings.ml_forward_bars,
        "long_threshold": settings.ml_label_threshold,
        "short_threshold": -settings.ml_label_threshold,
        "use_triple_barrier": settings.ml_use_triple_barrier,
        "barrier_max_bars": settings.ml_barrier_max_bars,
        "atr_stop_mult": settings.scalp_atr_stop_mult,
        "atr_take_mult": (
            settings.scalp_atr_stop_mult * settings.scalp_3r_min_rr
            if settings.scalp_3r_mode
            else settings.scalp_atr_take_mult
        ),
        "max_stop_pct": settings.scalp_max_stop_pct,
        "max_take_pct": (
            min(0.15, settings.scalp_max_stop_pct * settings.scalp_3r_min_rr * 1.05)
            if settings.scalp_3r_mode
            else settings.scalp_max_take_pct
        ),
        "harsh_move_only": settings.ml_harsh_move_only,
    }
