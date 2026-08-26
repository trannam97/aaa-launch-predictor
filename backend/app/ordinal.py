"""The ordinal predictor itself: how a fitted model turns features into tiers.

Split from `ml/train.py` for one practical reason. The API serves forecasts
from an artifact produced by the training job, and an artifact can only be
unpickled where its classes are importable. The backend runs from `backend/`
with `/ml` nowhere on its path, so the model class has to live here — beside
`features.py`, which builds the vectors it consumes.

What stays in `/ml` is everything about *producing* a trustworthy model:
cross-validation, the comparison against a constant guess, and the gate that
decides whether an artifact gets written at all.

**Why ordinal.** Flop < Underperform < Success < Breakout is an ordered
scale. Calling a breakout a success is a small error; calling it a flop is a
large one. A plain 4-way classifier treats both as equally wrong. This uses
the Frank & Hall decomposition — K-1 binary classifiers, each answering "is
the outcome above tier k?" — so the ordering is part of what the model is
asked to learn rather than something scored after the fact.

Importing this module requires numpy, and loading an artifact additionally
requires scikit-learn and joblib. Neither is a backend dependency; see
`app/model_forecast.py`, which treats their absence as "no model available"
and falls back to the rule-based baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.models import Outcome

TIERS = (Outcome.FLOP, Outcome.UNDERPERFORM, Outcome.SUCCESS, Outcome.BREAKOUT)
RANK_TO_TIER = {tier.rank: tier for tier in TIERS}

MODEL_VERSION = "ordinal_gbt_v1"
# Repo-relative: backend/app/ordinal.py -> repo root -> ml/models. A
# backend-only deployment simply won't find it, which `load()` treats as the
# ordinary "no model yet" case rather than an error.
MODEL_DIR = Path(__file__).resolve().parents[2] / "ml" / "models"
DEFAULT_ARTIFACT = MODEL_DIR / "outcome_model.joblib"

# Depth 2 expresses a single pairwise interaction and nothing more. At thirty
# rows anything deeper reconstructs individual games, which reads as skill in
# training and as noise everywhere else. The rest of these are the same
# argument: many small shrunken steps rather than a few confident ones.
#
# These are fixed a priori and never tuned against the numbers the evaluation
# reports. A search over depth or learning rate at this sample size finds the
# split that flatters the model, and the reported accuracy quietly becomes a
# training score.
TREE_PARAMS: dict[str, Any] = {
    "n_estimators": 150,
    "learning_rate": 0.05,
    "max_depth": 2,
    "min_samples_leaf": 3,
    "subsample": 0.8,
    "random_state": 0,
}


class OrdinalGradientBoosting:
    """Frank & Hall ordinal decomposition over gradient-boosted trees.

    Fits one binary classifier per threshold — P(outcome > flop),
    P(outcome > underperform), P(outcome > success) — and reassembles them
    into a distribution over the four tiers.
    """

    def __init__(self, **params: Any) -> None:
        self.params = {**TREE_PARAMS, **params}
        self.thresholds_: list[Any] = []
        self.n_features_: int = 0

    def fit(self, x: np.ndarray, y: np.ndarray) -> OrdinalGradientBoosting:
        """`y` holds tier ranks (0..3), not tier objects."""
        x = self._as_matrix(x)
        # Imported here rather than at module scope so that serving a stored
        # model does not drag the training stack into the API process.
        from sklearn.ensemble import GradientBoostingClassifier

        self.n_features_ = x.shape[1]
        self.thresholds_ = []
        for cut in range(len(TIERS) - 1):
            above = (y > cut).astype(int)
            # A fold can land entirely on one side of a cut — every game in it
            # above "flop", say. There is nothing to learn from that split, so
            # the threshold becomes the constant it observed rather than a
            # failed fit.
            if above.min() == above.max():
                self.thresholds_.append(float(above[0]))
                continue
            model = GradientBoostingClassifier(**self.params)
            model.fit(x, above)
            self.thresholds_.append(model)
        return self

    @staticmethod
    def _as_matrix(x: Any) -> np.ndarray:
        """Accept a plain list of feature values as well as an array.

        Training passes arrays; serving passes one row built from a `Game`.
        Coercing here keeps the serving path from having to know that.
        """
        return np.asarray(x, dtype=float)

    def _above_probabilities(self, x: np.ndarray) -> np.ndarray:
        columns = []
        for threshold in self.thresholds_:
            if isinstance(threshold, float):
                columns.append(np.full(x.shape[0], threshold))
            else:
                columns.append(threshold.predict_proba(x)[:, 1])
        return np.column_stack(columns)

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        x = self._as_matrix(x)
        above = self._above_probabilities(x)
        # P(y=0) = 1 - P(y>0); P(y=k) = P(y>k-1) - P(y>k); P(y=last) = P(y>last-1).
        padded = np.column_stack([np.ones(x.shape[0]), above, np.zeros(x.shape[0])])
        probabilities = padded[:, :-1] - padded[:, 1:]
        # The thresholds are fitted independently, so nothing forces
        # P(y>0) >= P(y>1) and a difference can come out negative. Clipping
        # and renormalising is the standard repair; it costs calibration at
        # the margin but keeps the output a distribution.
        probabilities = np.clip(probabilities, 1e-9, None)
        return probabilities / probabilities.sum(axis=1, keepdims=True)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.predict_proba(x).argmax(axis=1)


@dataclass(slots=True)
class Artifact:
    """A model plus everything needed to know whether to believe it.

    The cross-validation figures travel with the model rather than living in
    a report, so the endpoint serving a forecast can state how that model
    scored against a constant guess without anyone looking it up.
    """

    model: OrdinalGradientBoosting
    feature_names: tuple[str, ...]
    version: str
    trained_at: datetime
    n_rows: int
    cv_accuracy: float
    cv_distance: float
    constant_accuracy: float
    constant_distance: float
    label_fingerprint: str

    @property
    def method(self) -> str:
        return self.version


def save(artifact: Artifact, path: Path = DEFAULT_ARTIFACT) -> Path:
    import joblib

    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    return path


def load(path: Path = DEFAULT_ARTIFACT) -> Artifact | None:
    """Return the stored model, or None when there isn't one.

    None is a normal state, not an error: the training job refuses to write
    an artifact that has not beaten a constant guess, so callers fall back to
    the rule-based baseline and say which one they used.
    """
    if not path.exists():
        return None
    import joblib

    return joblib.load(path)
