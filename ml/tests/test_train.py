"""Tests for the ordinal classifier and, mostly, for the gate in front of it.

The model's own tests are the easy half: a distribution has to be a
distribution, and an ordered signal has to be learned in order. The half that
matters is `beats_constant()`, because that is the only thing standing
between a meaningless model and a confidence number on the dashboard. Noise
must not pass it.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.features import FEATURE_NAMES, TrainingRow
from app.models import Outcome
from app.ordinal import TIERS
from ml.train import (
    Evaluation,
    NotEnoughLabels,
    beats_constant,
    evaluate,
    train_final,
)

RNG = np.random.default_rng(11)


def row(index: int, outcome: Outcome, features: list[float] | None = None) -> TrainingRow:
    return TrainingRow(
        steam_appid=1000 + index,
        game_name=f"Game {index}",
        features=features if features is not None else [0.0] * len(FEATURE_NAMES),
        outcome=outcome,
    )


def separable_corpus(per_tier: int = 12) -> list[TrainingRow]:
    """A corpus where the first feature genuinely orders the outcome."""
    rows = []
    for tier in TIERS:
        for _ in range(per_tier):
            features = list(RNG.normal(0, 0.4, len(FEATURE_NAMES)))
            features[0] = tier.rank + RNG.normal(0, 0.25)
            rows.append(row(len(rows), tier, features))
    return rows


def noise_corpus(per_tier: int = 12) -> list[TrainingRow]:
    """Same shape, no signal: labels are unrelated to every feature."""
    rows = []
    for tier in TIERS:
        for _ in range(per_tier):
            rows.append(row(len(rows), tier, list(RNG.normal(0, 1.0, len(FEATURE_NAMES)))))
    return rows


def test_probabilities_form_a_distribution_over_the_four_tiers():
    rows = separable_corpus(8)
    model = train_final(rows)
    x = np.array([r.features for r in rows], dtype=float)
    probabilities = model.predict_proba(x)

    assert probabilities.shape == (len(rows), len(TIERS))
    assert np.allclose(probabilities.sum(axis=1), 1.0)
    assert (probabilities > 0).all()


def test_ordinal_errors_are_smaller_than_random_ones_when_signal_exists():
    # The point of the Frank & Hall decomposition: when the model is wrong it
    # should be wrong by one tier, not by three.
    rows = separable_corpus(12)
    result = evaluate(rows, repeats=4)
    assert result.model_distance < 0.5
    assert result.model_accuracy > 0.6


def test_a_one_sided_threshold_does_not_break_the_fit():
    # Every row above "flop" leaves the first binary classifier with a single
    # class. It should fall back to the constant it saw rather than raise.
    rows = [row(i, Outcome.SUCCESS, [float(i)] * len(FEATURE_NAMES)) for i in range(6)]
    rows += [row(10 + i, Outcome.BREAKOUT, [float(-i)] * len(FEATURE_NAMES)) for i in range(6)]
    model = train_final(rows)
    probabilities = model.predict_proba(np.array([r.features for r in rows], dtype=float))

    assert np.allclose(probabilities.sum(axis=1), 1.0)
    # Nothing in the training data was a flop, so essentially no mass there.
    assert probabilities[:, Outcome.FLOP.rank].max() < 0.01


def test_noise_does_not_pass_the_gate():
    result = evaluate(noise_corpus(12), repeats=4)
    assert not beats_constant(result)
    assert result.verdict != "beats the constant"


def test_signal_passes_the_gate():
    result = evaluate(separable_corpus(12), repeats=4)
    assert beats_constant(result)
    assert result.verdict == "beats the constant"


def test_being_ahead_on_the_mean_is_not_enough_to_pass():
    # A model that wins by less than its own uncertainty has shown nothing.
    marginal = Evaluation(
        n_rows=30,
        n_splits=5,
        n_repeats=20,
        tier_counts=dict.fromkeys(TIERS, 0),
        model_accuracy=0.40,
        model_distance=0.90,
        constant_accuracy=0.35,
        constant_distance=1.00,
        improvement=0.10,
        improvement_se=0.09,
    )
    assert marginal.improvement > 0
    assert not beats_constant(marginal)


def test_an_accuracy_regression_blocks_a_distance_win():
    # Closer on average but right less often is a trade the dashboard should
    # not make silently.
    lopsided = Evaluation(
        n_rows=30,
        n_splits=5,
        n_repeats=20,
        tier_counts=dict.fromkeys(TIERS, 0),
        model_accuracy=0.28,
        model_distance=0.70,
        constant_accuracy=0.35,
        constant_distance=1.00,
        improvement=0.30,
        improvement_se=0.05,
    )
    assert lopsided.improvement_ci_low > 0
    assert not beats_constant(lopsided)


def test_too_few_rows_is_refused_rather_than_scored():
    with pytest.raises(NotEnoughLabels):
        evaluate([row(i, TIERS[i % len(TIERS)]) for i in range(6)])


def test_a_tier_with_one_example_is_refused():
    def noisy() -> list[float]:
        return list(RNG.normal(0, 1, len(FEATURE_NAMES)))

    rows = [row(i, Outcome.SUCCESS, noisy()) for i in range(12)]
    rows += [row(20 + i, Outcome.FLOP, noisy()) for i in range(6)]
    rows.append(row(99, Outcome.BREAKOUT, noisy()))

    with pytest.raises(NotEnoughLabels, match="single labeled example"):
        evaluate(rows)


def test_every_row_is_scored_while_held_out():
    rows = separable_corpus(8)
    result = evaluate(rows, repeats=2)
    assert len(result.rows) == len(rows)
    assert {r.steam_appid for r in result.rows} == {r.steam_appid for r in rows}


def test_evaluation_is_deterministic():
    rows = separable_corpus(10)
    first = evaluate(rows, repeats=3)
    second = evaluate(rows, repeats=3)
    assert first.model_accuracy == second.model_accuracy
    assert first.model_distance == second.model_distance
