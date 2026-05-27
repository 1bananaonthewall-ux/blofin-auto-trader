from __future__ import annotations



import json

import logging

from dataclasses import asdict, dataclass, field, fields

from datetime import datetime, timezone

from pathlib import Path



import joblib

import numpy as np

from sklearn.ensemble import (

    ExtraTreesClassifier,

    HistGradientBoostingClassifier,

    RandomForestClassifier,

)

from sklearn.linear_model import LogisticRegression

from sklearn.metrics import accuracy_score



from ml.cv import PurgedTimeSeriesSplit

from ml.features import FEATURE_NAMES



log = logging.getLogger(__name__)





def _clamp(v: float, lo: float, hi: float) -> float:

    return max(lo, min(hi, v))





def _side_confidence_threshold(oos_precision: float, base: float = 0.55) -> float:

    """Higher OOS precision → can accept slightly lower live confidence."""

    return _clamp(base - (oos_precision - 0.5) * 0.12, 0.52, 0.78)





@dataclass

class ModelMetrics:

    trained_at: str

    samples: int

    symbols: int

    train_accuracy: float

    val_accuracy: float

    val_long_precision: float

    val_short_precision: float

    deployed: bool

    feature_names: list[str]

    walk_forward_splits: int = 5

    feedback_samples: int = 0

    ensemble_vote_threshold: float = 0.5

    ensemble_weights: list[float] = field(default_factory=lambda: [0.25, 0.25, 0.25, 0.25])

    min_long_confidence: float = 0.58

    min_short_confidence: float = 0.55





class _EnsembleModel:

    """Four-model ensemble with OOS-weighted probability fusion."""



    def __init__(self) -> None:

        self.gb = HistGradientBoostingClassifier(

            max_depth=5,

            learning_rate=0.05,

            max_iter=180,

            min_samples_leaf=50,

            l2_regularization=2.0,

            random_state=42,

        )

        self.rf = RandomForestClassifier(

            n_estimators=160,

            max_depth=7,

            min_samples_leaf=50,

            class_weight="balanced",

            random_state=43,

            n_jobs=-1,

        )

        self.lr = LogisticRegression(

            C=0.08,

            max_iter=1000,

            class_weight="balanced",

            random_state=44,

            n_jobs=-1,

        )

        self.et = ExtraTreesClassifier(

            n_estimators=120,

            max_depth=8,

            min_samples_leaf=45,

            class_weight="balanced",

            random_state=45,

            n_jobs=-1,

        )

        self.weights = np.array([0.25, 0.25, 0.25, 0.25], dtype=np.float64)



    def fit_all(self, X: np.ndarray, y: np.ndarray) -> None:

        self.gb.fit(X, y)

        self.rf.fit(X, y)

        self.lr.fit(X, y)

        self.et.fit(X, y)



    def predict_proba_all(self, X: np.ndarray) -> list[np.ndarray]:

        return [

            self.gb.predict_proba(X),

            self.rf.predict_proba(X),

            self.lr.predict_proba(X),

            self.et.predict_proba(X),

        ]



    def set_weights_from_oos(self, accs: list[float]) -> None:

        w = np.array([max(0.05, a) for a in accs], dtype=np.float64)

        self.weights = w / w.sum()



    def predict_ensemble(self, X: np.ndarray, threshold: float = 0.5) -> np.ndarray:

        avg = self.predict_proba_weighted(X)

        return np.argmax(avg, axis=1)



    def predict_proba_weighted(self, X: np.ndarray) -> np.ndarray:

        probas = self.predict_proba_all(X)

        out = np.zeros_like(probas[0])

        for i, p in enumerate(probas):

            out += self.weights[i] * p

        return out



    def predict_proba_avg(self, X: np.ndarray) -> np.ndarray:

        return self.predict_proba_weighted(X)



    def acc_and_prec(self, X: np.ndarray, y: np.ndarray, threshold: float) -> tuple:

        pred = self.predict_ensemble(X, threshold)

        acc = float(accuracy_score(y, pred))

        precs = {}

        for cls in (0, 1):

            mask = y == cls

            if mask.sum() > 0:

                correct = ((pred == cls) & (y == cls)).sum()

                precs[cls] = correct / mask.sum()

            else:

                precs[cls] = 0.0

        return acc, precs[0], precs[1]





class SignalModel:

    def __init__(self) -> None:

        self.ensemble = _EnsembleModel()

        self.metrics: ModelMetrics | None = None



    def fit(

        self,

        X: np.ndarray,

        y: np.ndarray,

        symbols: int,

        walk_forward_splits: int = 5,

        min_train_samples: int = 500,

        X_feedback: np.ndarray | None = None,

        y_feedback: np.ndarray | None = None,

        min_deploy_samples: int = 400,

        purge_gap: int = 30,

        embargo_pct: float = 0.01,

    ) -> ModelMetrics:

        self._min_deploy_samples = min_deploy_samples



        if X_feedback is not None and y_feedback is not None and len(y_feedback) > 0:

            if X_feedback.ndim == 2 and X_feedback.shape[1] == X.shape[1]:

                log.info("merging %d real-feedback samples into training", len(y_feedback))

                X = np.vstack([X, X_feedback])

                y = np.concatenate([y, y_feedback])

            else:

                log.warning("feedback shape mismatch, skipping merge")



        n_splits = max(2, walk_forward_splits)

        ptscv = PurgedTimeSeriesSplit(n_splits=n_splits, purge_gap=purge_gap, embargo_pct=embargo_pct)



        val_accs: list[float] = []

        val_long_p: list[float] = []

        val_short_p: list[float] = []

        model_accs: list[float] = []



        for train_idx, val_idx in ptscv.split(X, y):

            if len(train_idx) < min_train_samples:

                continue

            X_tr, X_va = X[train_idx], X[val_idx]

            y_tr, y_va = y[train_idx], y[val_idx]

            self.ensemble.fit_all(X_tr, y_tr)



            acc, lp, sp = self.ensemble.acc_and_prec(X_va, y_va, 0.5)

            val_accs.append(acc)

            val_long_p.append(lp)

            val_short_p.append(sp)

            per_model = []

            for proba in self.ensemble.predict_proba_all(X_va):

                pred = np.argmax(proba, axis=1)

                per_model.append(float(accuracy_score(y_va, pred)))

            model_accs.append(float(np.mean(per_model)))



        if not val_accs:

            split = int(len(X) * 0.8)

            X_tr, X_va = X[:split], X[split:]

            y_tr, y_va = y[:split], y[split:]

            self.ensemble.fit_all(X_tr, y_tr)

            acc, lp, sp = self.ensemble.acc_and_prec(X_va, y_va, 0.5)

            val_accs = [acc]

            val_long_p = [lp]

            val_short_p = [sp]

            model_accs = [acc]



        if model_accs:

            self.ensemble.set_weights_from_oos(model_accs)



        self.ensemble.fit_all(X, y)



        train_pred = self.ensemble.predict_ensemble(X, 0.5)

        train_acc = float(accuracy_score(y, train_pred))



        avg_val_acc = float(np.mean(val_accs)) if val_accs else 0.0

        avg_long_p = float(np.mean(val_long_p)) if val_long_p else 0.0

        avg_short_p = float(np.mean(val_short_p)) if val_short_p else 0.0



        min_val_acc = 0.54

        min_precision = 0.32

        min_samples = getattr(self, "_min_deploy_samples", 400)

        overfit_gap = train_acc - avg_val_acc

        deployed = (

            avg_val_acc >= min_val_acc

            and len(X) >= min_samples

            and (avg_long_p >= min_precision or avg_short_p >= min_precision)

            and overfit_gap < 0.35

        )



        min_long_c = _side_confidence_threshold(avg_long_p, 0.58)

        min_short_c = _side_confidence_threshold(avg_short_p, 0.55)



        self.metrics = ModelMetrics(

            trained_at=datetime.now(timezone.utc).isoformat(),

            samples=int(len(X)),

            symbols=symbols,

            train_accuracy=train_acc,

            val_accuracy=avg_val_acc,

            val_long_precision=avg_long_p,

            val_short_precision=avg_short_p,

            deployed=deployed,

            feature_names=FEATURE_NAMES,

            walk_forward_splits=n_splits,

            feedback_samples=len(y_feedback) if y_feedback is not None else 0,

            ensemble_vote_threshold=0.5,

            ensemble_weights=[float(w) for w in self.ensemble.weights],

            min_long_confidence=min_long_c,

            min_short_confidence=min_short_c,

        )

        return self.metrics



    def predict_proba(self, X: np.ndarray) -> np.ndarray:

        return self.ensemble.predict_proba_weighted(X)



    def save(self, model_path: Path, meta_path: Path) -> None:

        model_path.parent.mkdir(parents=True, exist_ok=True)

        artifacts = {

            "gb": self.ensemble.gb,

            "rf": self.ensemble.rf,

            "lr": self.ensemble.lr,

            "et": self.ensemble.et,

            "weights": self.ensemble.weights,

        }

        joblib.dump(artifacts, model_path)

        if self.metrics:

            meta_path.write_text(json.dumps(asdict(self.metrics), indent=2), encoding="utf-8")



    @classmethod

    def load(cls, model_path: Path, meta_path: Path) -> SignalModel | None:

        if not model_path.exists():

            return None

        obj = cls()

        try:

            artifacts = joblib.load(model_path)

            if isinstance(artifacts, dict) and "gb" in artifacts:

                obj.ensemble.gb = artifacts["gb"]

                obj.ensemble.rf = artifacts["rf"]

                obj.ensemble.lr = artifacts["lr"]

                if "et" in artifacts:

                    obj.ensemble.et = artifacts["et"]

                else:

                    obj.ensemble.et.fit(

                        np.zeros((2, len(FEATURE_NAMES))),

                        np.array([0, 1]),

                    )

                if "weights" in artifacts:

                    w = np.asarray(artifacts["weights"], dtype=np.float64)

                    if w.shape == (4,):

                        obj.ensemble.weights = w / max(w.sum(), 1e-9)

            else:

                obj.ensemble.gb = artifacts

        except Exception:

            log.warning("failed to load model, need fresh training")

            return None

        if meta_path.exists():

            raw = json.loads(meta_path.read_text(encoding="utf-8"))

            valid = {f.name for f in fields(ModelMetrics)}

            obj.metrics = ModelMetrics(**{k: v for k, v in raw.items() if k in valid})

        return obj


