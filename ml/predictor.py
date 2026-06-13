from __future__ import annotations



import logging

from pathlib import Path



import numpy as np



from ml.features import FEATURE_NAMES, build_feature_vector

from ml.trainer import SignalModel

from strategy import Signal, StrategyDecision



log = logging.getLogger(__name__)





class MLPredictor:

    def __init__(

        self,

        state_dir: Path,

        *,

        min_confidence: float = 0.55,

        min_score: float = 50.0,

    ) -> None:

        self.model_path = state_dir / "signal_model.joblib"

        self.meta_path = state_dir / "signal_model_meta.json"

        self.state_dir = state_dir

        self.min_confidence = min_confidence

        self.min_score = min_score

        self.model: SignalModel | None = None

        try:

            self.reload()

        except Exception:

            log.warning("could not load saved ML model (version mismatch?) - will train fresh")



    def reload(self) -> bool:

        try:

            self.model = SignalModel.load(self.model_path, self.meta_path)

            if (
                self.model
                and self.model.metrics
                and len(self.model.metrics.feature_names) != len(FEATURE_NAMES)
            ):
                log.warning(
                    "ML feature count %d != code %d — need refit",
                    len(self.model.metrics.feature_names),
                    len(FEATURE_NAMES),
                )
                self.model.metrics.deployed = False

        except Exception:

            log.warning("model reload failed, need fresh training")

            self.model = None

        return self.is_ready()



    def is_ready(self) -> bool:

        return (

            self.model is not None

            and self.model.metrics is not None

            and self.model.metrics.deployed

        )



    def side_min_confidence(self, side: Signal) -> float:

        if not self.model or not self.model.metrics:

            return self.min_confidence

        m = self.model.metrics

        if side == Signal.LONG:

            return max(self.min_confidence, m.min_long_confidence)

        if side == Signal.SHORT:

            return max(self.min_confidence, m.min_short_confidence)

        return self.min_confidence



    def metrics_summary(self) -> str:

        if not self.model or not self.model.metrics:

            return "no model"

        m = self.model.metrics

        return (

            f"samples={m.samples} val_acc={m.val_accuracy:.1%} "

            f"long_p={m.val_long_precision:.1%} short_p={m.val_short_precision:.1%} "

            f"long_c>={m.min_long_confidence:.2f} short_c>={m.min_short_confidence:.2f}"

        )



    def predict(

        self,

        ohlcv_1m: list[list[float]],

        ohlcv_5m: list[list[float]],

        *,

        funding_rate: float | None = None,

        atr_stop_mult: float = 1.8,

        atr_take_mult: float = 3.5,

    ) -> StrategyDecision | None:

        from indicators import atr



        feats = build_feature_vector(ohlcv_1m, ohlcv_5m, funding_rate=funding_rate)

        if feats is None:

            return None



        close = ohlcv_1m[-1][4]

        atr_v = atr(ohlcv_1m, 14)

        if atr_v is None or close <= 0:

            stop_pct, take_pct = 0.02, 0.04

        else:

            atr_pct = atr_v / close

            stop_pct = min(0.05, max(0.01, atr_pct * atr_stop_mult))

            take_pct = min(0.15, max(stop_pct * 2.0, atr_pct * atr_take_mult))



        if not self.is_ready():

            return None



        n_feat = len(feats)

        meta_n = len(self.model.metrics.feature_names) if self.model.metrics else n_feat

        if meta_n != n_feat:

            log.warning(

                "feature mismatch model=%d code=%d — run: python train_model.py",

                meta_n,

                n_feat,

            )

            return None

        proba = self.model.predict_proba(feats.reshape(1, -1))[0]

        p_long = float(proba[0]) if len(proba) > 0 else 0.0

        p_short = float(proba[1]) if len(proba) > 1 else 0.0



        signal = Signal.FLAT

        confidence = max(p_long, p_short)

        margin = abs(p_long - p_short)



        long_min = self.side_min_confidence(Signal.LONG)

        short_min = self.side_min_confidence(Signal.SHORT)

        if p_long >= long_min and margin >= 0.10:

            signal = Signal.LONG

            confidence = p_long

        elif p_short >= short_min and margin >= 0.10:

            signal = Signal.SHORT

            confidence = p_short



        score = confidence * 100

        if score < self.min_score:

            signal = Signal.FLAT



        return StrategyDecision(

            signal=signal,

            score=round(score, 2),

            fast_ema=0.0,

            slow_ema=0.0,

            rsi=50.0,

            close=close,

            stop_pct=stop_pct,

            take_pct=take_pct,

            volume_ratio=1.0,

            htf_aligned=True,

            funding_rate=funding_rate,

            model_confidence=confidence,

            regime="normal",

            vwap_distance_pct=0.0,

        )



    def predict_proba_pair(

        self,

        ohlcv_1m: list[list[float]],

        ohlcv_5m: list[list[float]],

        *,

        funding_rate: float | None = None,

    ) -> tuple[float, float] | None:

        if not self.is_ready():

            return None

        feats = build_feature_vector(ohlcv_1m, ohlcv_5m, funding_rate=funding_rate)

        if feats is None:

            return None

        if self.model.metrics and len(self.model.metrics.feature_names) != len(feats):

            return None

        proba = self.model.predict_proba(feats.reshape(1, -1))[0]

        p_long = float(proba[0]) if len(proba) > 0 else 0.0

        p_short = float(proba[1]) if len(proba) > 1 else 0.0

        try:
            from ml.calibration import calibrate_pair

            p_long, p_short = calibrate_pair(p_long, p_short, self.state_dir)
        except Exception:
            pass
        return p_long, p_short


