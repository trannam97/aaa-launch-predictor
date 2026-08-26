"""How a candidate model is measured, and what it has to beat to be served.

The model class itself is in `app/ordinal.py`, beside the code that serves
it. What lives here is the part that decides whether it deserves to exist.

**The evaluation is allowed to reject the model.** At this corpus size a
single train/test split is noise — an eight-row test set moves ~12 points per
row — so nothing here reports one. `evaluate()` runs repeated stratified
k-fold, scores every row while it is held out, and compares against the best
trivial guess computed *inside each training fold*. `beats_constant()` is the
gate, and `jobs/train_model.py` writes no artifact unless it passes. A model
that cannot beat a constant guess has learned nothing, and shipping it behind
a confidence number would be worse than shipping nothing.

The comparison is against a constant rather than against the rule-based
baseline in `app/baseline.py`. The baseline does not clear the constant
either, so beating it would prove nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.model_selection import RepeatedStratifiedKFold

from app.features import FEATURE_NAMES, TrainingRow, assert_no_leakage
from app.models import Outcome
from app.ordinal import (
    MODEL_DIR,
    MODEL_VERSION,
    RANK_TO_TIER,
    TIERS,
    Artifact,
    OrdinalGradientBoosting,
)

# Written on every run, including the ones that refuse to write a model.
# Without it a failed gate leaves no record, and the biweekly job has nothing
# to compare against to know whether the labels have moved.
DEFAULT_RUN_RECORD = MODEL_DIR / "last_run.json"

# Repeats average away the arbitrariness of any one fold assignment. Twenty
# is well past the point where the reported means stop moving, and the whole
# run still takes seconds at this corpus size.
CV_REPEATS = 20
MAX_SPLITS = 5


class NotEnoughLabels(RuntimeError):
    """The labeled set is too small or too lopsided to evaluate honestly."""


@dataclass(slots=True)
class RowScore:
    """How one game fared, averaged over every repeat that held it out."""

    steam_appid: int
    game_name: str
    actual: Outcome
    model_accuracy: float
    model_distance: float
    constant_distance: float
    modal_prediction: Outcome


@dataclass(slots=True)
class Evaluation:
    n_rows: int
    n_splits: int
    n_repeats: int
    tier_counts: dict[Outcome, int]
    model_accuracy: float
    model_distance: float
    constant_accuracy: float
    constant_distance: float
    improvement: float
    improvement_se: float
    rows: list[RowScore] = field(default_factory=list)

    @property
    def improvement_ci_low(self) -> float:
        """Lower bound of a 95% interval on the ordinal-distance improvement."""
        return self.improvement - 1.96 * self.improvement_se

    @property
    def verdict(self) -> str:
        if beats_constant(self):
            return "beats the constant"
        if self.improvement > 0:
            return "better than the constant, but inside the noise"
        return "does not beat the constant"


def beats_constant(evaluation: Evaluation) -> bool:
    """Whether the model has earned the right to be served.

    Two conditions, both necessary. The ordinal-distance improvement has to
    clear its own uncertainty — being ahead on the mean is not evidence when
    the interval straddles zero — and accuracy must not be worse than simply
    guessing the most common tier every time.

    The interval is a paired comparison over held-out rows, which is the
    right unit: every row is scored by a model that never saw it. It is still
    optimistic, because folds share training rows and the per-row errors are
    therefore not independent. That argues for treating a marginal pass as
    marginal, not for a looser test.
    """
    if evaluation.n_rows == 0:
        return False
    return evaluation.improvement_ci_low > 0 and evaluation.model_accuracy >= (
        evaluation.constant_accuracy
    )


def _constant_by_mode(y: np.ndarray) -> int:
    """The tier that maximises accuracy: the most common one."""
    return int(np.bincount(y, minlength=len(TIERS)).argmax())


def _constant_by_median(y: np.ndarray) -> int:
    """The tier that minimises mean ordinal distance: the median one.

    Worth separating from the mode. Comparing an ordinal model against the
    modal guess on distance would be scoring it against a straw man — the
    median is the trivial guess that is actually hard to beat on distance,
    and it is often a different tier.
    """
    return int(np.median(y))


def _choose_splits(y: np.ndarray) -> int:
    present = np.bincount(y, minlength=len(TIERS))
    smallest = int(present[present > 0].min())
    if smallest < 2:
        raise NotEnoughLabels(
            "at least one outcome tier has a single labeled example; stratified "
            "cross-validation cannot hold it out and still train on it"
        )
    return min(MAX_SPLITS, smallest)


def to_matrix(rows: list[TrainingRow]) -> tuple[np.ndarray, np.ndarray]:
    assert_no_leakage()
    x = np.array([row.features for row in rows], dtype=float)
    y = np.array([row.outcome.rank for row in rows], dtype=int)
    return x, y


def evaluate(rows: list[TrainingRow], *, repeats: int = CV_REPEATS) -> Evaluation:
    """Repeated stratified k-fold over every labeled row.

    Nothing here is measured on a row the model trained on, and the constant
    it is compared against is recomputed inside each training fold — a
    constant derived from the full labeled set would already know the answer
    distribution of the rows it is being scored on.
    """
    if len(rows) < len(TIERS) * 2:
        raise NotEnoughLabels(
            f"{len(rows)} labeled rows is too few to cross-validate four tiers; "
            "the fold estimates would be wider than any effect they could show"
        )

    x, y = to_matrix(rows)
    n_splits = _choose_splits(y)

    hits = np.zeros(len(rows))
    constant_hits = np.zeros(len(rows))
    model_distance = np.zeros(len(rows))
    constant_distance = np.zeros(len(rows))
    held_out = np.zeros(len(rows))
    votes = np.zeros((len(rows), len(TIERS)))

    splitter = RepeatedStratifiedKFold(n_splits=n_splits, n_repeats=repeats, random_state=0)
    for train_index, test_index in splitter.split(x, y):
        model = OrdinalGradientBoosting().fit(x[train_index], y[train_index])
        predicted = model.predict(x[test_index])

        modal = _constant_by_mode(y[train_index])
        median = _constant_by_median(y[train_index])
        actual = y[test_index]

        hits[test_index] += predicted == actual
        model_distance[test_index] += np.abs(predicted - actual)
        # Accuracy is scored against the modal constant and distance against
        # the median one, so each comparison faces the strongest trivial guess
        # for that metric rather than one convenient straw man.
        constant_hits[test_index] += actual == modal
        constant_distance[test_index] += np.abs(median - actual)
        held_out[test_index] += 1
        votes[test_index, predicted] += 1

    per_row_accuracy = hits / held_out
    per_row_constant_accuracy = constant_hits / held_out
    per_row_model = model_distance / held_out
    per_row_constant = constant_distance / held_out
    difference = per_row_constant - per_row_model

    scored = [
        RowScore(
            steam_appid=row.steam_appid,
            game_name=row.game_name,
            actual=row.outcome,
            model_accuracy=float(per_row_accuracy[i]),
            model_distance=float(per_row_model[i]),
            constant_distance=float(per_row_constant[i]),
            modal_prediction=RANK_TO_TIER[int(votes[i].argmax())],
        )
        for i, row in enumerate(rows)
    ]

    counts: dict[Outcome, int] = dict.fromkeys(TIERS, 0)
    for row in rows:
        counts[row.outcome] += 1

    return Evaluation(
        n_rows=len(rows),
        n_splits=n_splits,
        n_repeats=repeats,
        tier_counts=counts,
        model_accuracy=float(per_row_accuracy.mean()),
        model_distance=float(per_row_model.mean()),
        constant_accuracy=float(per_row_constant_accuracy.mean()),
        constant_distance=float(per_row_constant.mean()),
        improvement=float(difference.mean()),
        improvement_se=(
            float(difference.std(ddof=1) / math.sqrt(len(rows))) if len(rows) > 1 else 0.0
        ),
        rows=scored,
    )


def train_final(rows: list[TrainingRow]) -> OrdinalGradientBoosting:
    """Fit on every labeled row, for serving.

    Only called once the gate has passed. Cross-validation measured the
    procedure; this is the model that procedure produces when it is given all
    the evidence there is.
    """
    x, y = to_matrix(rows)
    return OrdinalGradientBoosting().fit(x, y)


def fingerprint(rows: list[TrainingRow]) -> str:
    """A stable digest of which games carry which labels.

    The retraining job skips when this is unchanged. It covers appids and
    outcomes only — a corrected label or a newly resolved game changes it, a
    refreshed Steam price does not. That is the intended reading of the
    spec's "no new resolved games": the training signal is the labels.
    """
    payload = sorted((row.steam_appid, row.outcome.value) for row in rows)
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    return f"{len(rows)}:{digest[:16]}"


def record_run(
    evaluation: Evaluation,
    label_fingerprint: str,
    *,
    artifact_written: bool,
    path: Path = DEFAULT_RUN_RECORD,
) -> Path:
    """Leave a record of what this run measured, whether or not it shipped.

    A refused model is the interesting case: without this file the next run
    would have nothing to compare labels against and would retrain an
    unchanged corpus every fortnight.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ran_at": datetime.now(UTC).isoformat(),
                "version": MODEL_VERSION,
                "label_fingerprint": label_fingerprint,
                "n_rows": evaluation.n_rows,
                "model_accuracy": round(evaluation.model_accuracy, 4),
                "model_distance": round(evaluation.model_distance, 4),
                "constant_accuracy": round(evaluation.constant_accuracy, 4),
                "constant_distance": round(evaluation.constant_distance, 4),
                "improvement": round(evaluation.improvement, 4),
                "improvement_ci_low": round(evaluation.improvement_ci_low, 4),
                "verdict": evaluation.verdict,
                "artifact_written": artifact_written,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def last_run(path: Path = DEFAULT_RUN_RECORD) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A corrupt record should cost a retrain, not a crash.
        return None


def build_artifact(rows: list[TrainingRow], evaluation: Evaluation) -> Artifact:
    return Artifact(
        model=train_final(rows),
        feature_names=FEATURE_NAMES,
        version=MODEL_VERSION,
        trained_at=datetime.now(UTC),
        n_rows=evaluation.n_rows,
        cv_accuracy=evaluation.model_accuracy,
        cv_distance=evaluation.model_distance,
        constant_accuracy=evaluation.constant_accuracy,
        constant_distance=evaluation.constant_distance,
        label_fingerprint=fingerprint(rows),
    )
