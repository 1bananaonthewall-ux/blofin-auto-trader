from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from indicators import (
    adx,
    atr,
    bollinger_bands,
    chaikin_money_flow,
    ema,
    macd,
    mfi,
    roc,
    rsi,
    stochastic_k,
    volume_ratio,
    williams_r,
)

FEATURE_NAMES = [
    "ret_1",
    "ret_5",
    "ret_15",
    "ret_30",
    "rsi_norm",
    "ema_spread_pct",
    "atr_pct",
    "vol_ratio",
    "htf_spread_pct",
    "funding_bps",
    "hour_sin",
    "hour_cos",
    # New features
    "macd_hist_norm",
    "macd_signal_spread",
    "bb_pct",
    "bb_width_pct",
    "adx",
    "mfi_norm",
    "chaikin_omf",
    "wick_upper_pct",
    "wick_lower_pct",
    "price_accel_3",
    "volume_accel_3",
    "atr_expansion",
    "ret_60",
    "rsi_slope",
    "trend_consistency",
    "vol_zscore",
    "range_position",
    "htf_rsi_norm",
    # Crypto Foretell-inspired: trend residual + return autocorrelation + HTF micro-align
    "trend_residual_pct",
    "ret_autocorr_1",
    "ret_autocorr_5",
    "micro_htf_align",
    # MDPI multivariate TA + IEEE trend-engineering proxies
    "williams_r_norm",
    "roc_10_norm",
    "stoch_k_norm",
    "ema_cascade",
    "intra_bar_vol_pct",
    "momentum_fusion",
    # Runner vs chop — same signals as run_quality filter (forward-training feedback)
    "path_efficiency",
    "chop_index",
    "runner_score",
]


def _returns(closes: list[float], n: int) -> float:
    if len(closes) <= n or closes[-n - 1] == 0:
        return 0.0
    return (closes[-1] - closes[-n - 1]) / closes[-n - 1]


def _ret_autocorr(closes: list[float], lag: int) -> float:
    """Short-lag return autocorrelation — long-horizon memory proxy (Foretell auto-corr branch)."""
    n = min(20, len(closes) - lag - 1)
    if n < 5:
        return 0.0
    rs1: list[float] = []
    rs2: list[float] = []
    base = len(closes)
    for k in range(n):
        idx = base - n + k
        if idx < 1 or idx - lag < 1:
            continue
        if closes[idx - 1] == 0 or closes[idx - lag - 1] == 0:
            continue
        rs1.append((closes[idx] - closes[idx - 1]) / closes[idx - 1])
        rs2.append((closes[idx - lag] - closes[idx - lag - 1]) / closes[idx - lag - 1])
    if len(rs1) < 4:
        return 0.0
    c = float(np.corrcoef(rs1, rs2)[0, 1])
    return 0.0 if math.isnan(c) else c


def build_feature_vector(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]],
    *,
    funding_rate: float | None = None,
    timestamp_ms: int | None = None,
) -> np.ndarray | None:
    if len(ohlcv_1m) < 55:
        return None

    closes = [row[4] for row in ohlcv_1m]
    volumes = [row[5] if len(row) > 5 else 0.0 for row in ohlcv_1m]
    i = len(closes) - 1
    close = closes[i]

    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rs = rsi(closes, 14)
    if fast[i] is None or slow[i] is None or rs[i] is None:
        return None

    atr_v = atr(ohlcv_1m, 14) or 0.0
    atr_pct_val = atr_v / close if close else 0.0
    ema_spread_val = (fast[i] - slow[i]) / close if close else 0.0

    htf_spread_val = 0.0
    if len(ohlcv_5m) >= 22:
        c5 = [row[4] for row in ohlcv_5m]
        f5 = ema(c5, 9)
        s5 = ema(c5, 21)
        j = len(c5) - 1
        if f5[j] is not None and s5[j] is not None and c5[j]:
            htf_spread_val = (f5[j] - s5[j]) / c5[j]

    ts = timestamp_ms or int(ohlcv_1m[-1][0])
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60
    hour_sin_val = math.sin(2 * math.pi * hour / 24)
    hour_cos_val = math.cos(2 * math.pi * hour / 24)

    funding_bps_val = (funding_rate or 0.0) * 10000

    # ---- NEW FEATURES ----

    # MACD
    macd_line, sig_line, hist = macd(closes)
    macd_hist_val = (hist[i] / close * 100) if hist[i] is not None and close else 0.0
    macd_sig_spread_val = ((macd_line[i] - sig_line[i]) / close * 100) if macd_line[i] is not None and sig_line[i] is not None and close else 0.0

    # Bollinger Bands %b and width
    bb_mid, bb_upper, bb_lower, bb_pct_arr = bollinger_bands(closes)
    bb_pct_val = bb_pct_arr[i] if bb_pct_arr[i] is not None else 0.5
    bb_width_val = ((bb_upper[i] - bb_lower[i]) / bb_mid[i]) if bb_upper[i] is not None and bb_lower[i] is not None and bb_mid[i] is not None and bb_mid[i] else 0.0

    # ADX - trend strength
    adx_val = adx(ohlcv_1m) or 0.0

    # MFI - money flow index
    mfi_val_raw = mfi(ohlcv_1m) or 50.0
    mfi_norm_val = (mfi_val_raw - 50) / 50

    # Chaikin Money Flow
    cmf_val = chaikin_money_flow(ohlcv_1m) or 0.0

    # Wick analysis
    high = ohlcv_1m[i][2]
    low = ohlcv_1m[i][3]
    o = ohlcv_1m[i][1]
    candle_range = high - low
    if candle_range > 0:
        wick_up_val = (high - max(o, close)) / candle_range
        wick_low_val = (min(o, close) - low) / candle_range
    else:
        wick_up_val = 0.0
        wick_low_val = 0.0

    # Price acceleration over last 3 bars
    if len(closes) >= 5:
        accel3 = (closes[-1] - closes[-2]) - (closes[-2] - closes[-3])
        price_accel_val = accel3 / close if close else 0.0
    else:
        price_accel_val = 0.0

    # Volume acceleration
    if len(volumes) >= 5:
        vol_accel3 = (volumes[-1] - volumes[-2]) - (volumes[-2] - volumes[-3])
        avg_vol = sum(volumes[-5:]) / 5
        volume_accel_val = vol_accel3 / avg_vol if avg_vol > 0 else 0.0
    else:
        volume_accel_val = 0.0

    # ATR expansion ratio
    atr_28 = atr(ohlcv_1m, 28) or atr_v
    atr_exp_val = (atr_v / atr_28 - 1.0) if atr_28 > 0 else 0.0

    ret_60_val = _returns(closes, min(60, len(closes) - 1))
    rsi_slope_val = 0.0
    if i >= 3 and rs[i] is not None and rs[i - 3] is not None:
        rsi_slope_val = (rs[i] - rs[i - 3]) / 50.0
    trend_consistency_val = 0.0
    if len(closes) >= 10:
        ups = sum(1 for j in range(-9, 0) if closes[j] > closes[j - 1])
        trend_consistency_val = (ups / 9.0 - 0.5) * 2.0
    vol_z_val = 0.0
    if len(volumes) >= 20:
        vwin = volumes[-20:]
        vm = sum(vwin) / len(vwin)
        vs = (sum((v - vm) ** 2 for v in vwin) / len(vwin)) ** 0.5
        vol_z_val = (volumes[-1] - vm) / vs if vs > 0 else 0.0
    range_pos_val = 0.5
    if len(ohlcv_1m) >= 20:
        highs = [row[2] for row in ohlcv_1m[-20:]]
        lows = [row[3] for row in ohlcv_1m[-20:]]
        hi, lo = max(highs), min(lows)
        if hi > lo:
            range_pos_val = (close - lo) / (hi - lo)
    htf_rsi_norm_val = 0.0
    if len(ohlcv_5m) >= 20:
        c5 = [row[4] for row in ohlcv_5m]
        rs5 = rsi(c5, 14)
        if rs5[-1] is not None:
            htf_rsi_norm_val = (rs5[-1] - 50) / 50

    trend_ma = ema(closes, 30)
    trend_residual_val = 0.0
    if trend_ma[i] is not None and close:
        trend_residual_val = (close - trend_ma[i]) / close
    ret_autocorr_1_val = _ret_autocorr(closes, 1)
    ret_autocorr_5_val = _ret_autocorr(closes, 5)
    r5 = _returns(closes, 5)
    micro_align = 0.0
    if abs(r5) > 1e-6 and abs(htf_spread_val) > 1e-6:
        micro_align = 1.0 if (r5 > 0) == (htf_spread_val > 0) else -1.0

    wr = williams_r(ohlcv_1m, 14)
    williams_r_norm_val = ((wr + 50) / 50.0) if wr is not None else 0.0
    roc_v = roc(closes, 10)
    roc_10_norm_val = roc_v * 100 if roc_v is not None else 0.0
    sk = stochastic_k(ohlcv_1m, 14)
    stoch_k_norm_val = (sk - 50) / 50.0 if sk is not None else 0.0

    ema_cascade_val = 0.0
    e7 = ema(closes, 7)
    e21 = ema(closes, 21)
    if e7[i] is not None and e21[i] is not None:
        if close >= e7[i] >= e21[i]:
            ema_cascade_val = 1.0
        elif close <= e7[i] <= e21[i]:
            ema_cascade_val = -1.0
    if len(closes) >= 50:
        e50 = ema(closes, 50)
        if (
            e7[i] is not None
            and e21[i] is not None
            and e50[i] is not None
            and close >= e7[i] >= e21[i] >= e50[i]
        ):
            ema_cascade_val = 1.0
        elif (
            e7[i] is not None
            and e21[i] is not None
            and e50[i] is not None
            and close <= e7[i] <= e21[i] <= e50[i]
        ):
            ema_cascade_val = -1.0

    o = ohlcv_1m[i][1]
    intra_bar_vol_val = abs(close - o) / close if close else 0.0
    momentum_fusion_val = (
        (rs[i] - 50) / 50.0 * 0.35
        + macd_hist_val * 0.25
        + roc_10_norm_val * 0.2
        + trend_consistency_val * 0.2
    )

    path_eff_val = 0.5
    chop_val = 0.5
    runner_val = 0.5
    try:
        from run_quality import measure_run_quality

        rq = measure_run_quality(ohlcv_1m, ohlcv_5m)
        if rq:
            path_eff_val = rq.path_efficiency_1m
            chop_val = rq.chop_index
            runner_val = rq.runner_score
    except Exception:
        pass

    return np.array(
        [
            _returns(closes, 1),
            _returns(closes, 5),
            _returns(closes, 15),
            _returns(closes, 30),
            (rs[i] - 50) / 50,
            ema_spread_val,
            atr_pct_val,
            volume_ratio(volumes) - 1.0,
            htf_spread_val,
            funding_bps_val,
            hour_sin_val,
            hour_cos_val,
            # New features
            macd_hist_val,
            macd_sig_spread_val,
            bb_pct_val,
            bb_width_val,
            adx_val,
            mfi_norm_val,
            cmf_val,
            wick_up_val,
            wick_low_val,
            price_accel_val,
            volume_accel_val,
            atr_exp_val,
            ret_60_val,
            rsi_slope_val,
            trend_consistency_val,
            vol_z_val,
            range_pos_val,
            htf_rsi_norm_val,
            trend_residual_val,
            ret_autocorr_1_val,
            ret_autocorr_5_val,
            micro_align,
            williams_r_norm_val,
            roc_10_norm_val,
            stoch_k_norm_val,
            ema_cascade_val,
            intra_bar_vol_val,
            momentum_fusion_val,
            path_eff_val,
            chop_val,
            runner_val,
        ],
        dtype=np.float64,
    )


def build_training_matrix(
    ohlcv_1m: list[list[float]],
    ohlcv_5m: list[list[float]],
    *,
    forward_bars: int = 5,
    long_threshold: float = 0.0015,
    short_threshold: float = -0.0015,
    funding_rate: float | None = None,
    use_triple_barrier: bool = True,
    barrier_max_bars: int = 30,
    atr_stop_mult: float = 1.15,
    atr_take_mult: float = 3.0,
    max_stop_pct: float = 0.022,
    max_take_pct: float = 0.066,
    harsh_move_only: bool = True,
    min_samples: int = 20,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Build (X, y). y: 0=long winner, 1=short winner (3R triple-barrier when enabled)."""
    from ml.labels import triple_barrier_direction
    from ml.regime_labels import cluster_forward_moves, harsh_move_cluster

    horizon = max(forward_bars, barrier_max_bars if use_triple_barrier else forward_bars)
    min_bars = max(55, 30)
    if len(ohlcv_1m) < min_bars + horizon:
        return None

    rows_x: list[np.ndarray] = []
    rows_y: list[int] = []
    pending: list[tuple[np.ndarray, int, float]] = []
    for end in range(min_bars, len(ohlcv_1m) - horizon):
        slice_1m = ohlcv_1m[: end + 1]
        ts = int(slice_1m[-1][0])
        slice_5m = [bar for bar in ohlcv_5m if int(bar[0]) <= ts]
        if len(slice_5m) < 22:
            continue

        feats = build_feature_vector(slice_1m, slice_5m, funding_rate=funding_rate, timestamp_ms=ts)
        if feats is None:
            continue

        c0 = slice_1m[-1][4]
        if c0 <= 0:
            continue

        if use_triple_barrier:
            atr_v = atr(slice_1m, 14) or c0 * 0.01
            stop_pct = min(max_stop_pct, max(0.004, (atr_v / c0) * atr_stop_mult))
            take_pct = min(max_take_pct, max(stop_pct * 3.0, (atr_v / c0) * atr_take_mult))
            label = triple_barrier_direction(
                ohlcv_1m,
                end,
                max_bars=barrier_max_bars,
                stop_pct=stop_pct,
                take_pct=take_pct,
            )
            if label is None:
                continue
            c1 = ohlcv_1m[min(end + barrier_max_bars, len(ohlcv_1m) - 1)][4]
            fwd = (c1 - c0) / c0
            pending.append((feats, label, fwd))
        else:
            c1 = ohlcv_1m[end + forward_bars][4]
            fwd = (c1 - c0) / c0
            if fwd >= long_threshold:
                label = 0
            elif fwd <= short_threshold:
                label = 1
            else:
                continue
            rows_x.append(feats)
            rows_y.append(label)

    if use_triple_barrier and pending:
        moves = np.array([p[2] for p in pending], dtype=np.float64)
        harsh_id = harsh_move_cluster(moves, k=3) if harsh_move_only else -1
        cluster_ids = cluster_forward_moves(moves, k=3) if harsh_move_only else None
        for i, (feats, label, _fwd) in enumerate(pending):
            if harsh_move_only and cluster_ids is not None and int(cluster_ids[i]) != harsh_id:
                continue
            rows_x.append(feats)
            rows_y.append(label)

    if len(rows_x) < max(8, min_samples):
        return None

    return np.vstack(rows_x), np.array(rows_y, dtype=np.int64)