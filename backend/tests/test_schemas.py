"""Tests for the status badge — the user-visible framing of an outcome."""

from __future__ import annotations

import pytest
from app.models import Game, LifecycleStatus, Outcome
from app.schemas import PROVISIONAL_LABEL, status_badge


def make_game(**kwargs) -> Game:
    game = Game(steam_appid=1, name="Example", lifecycle_status=LifecycleStatus.PRE_LAUNCH)
    for key, value in kwargs.items():
        setattr(game, key, value)
    return game


def test_pre_launch_without_a_forecast():
    badge = status_badge(make_game())

    assert badge.label == "Awaiting forecast"
    assert badge.provisional is False


def test_pre_launch_with_a_forecast_reads_as_a_forecast():
    badge = status_badge(make_game(predicted_outcome=Outcome.BREAKOUT))

    assert badge.label == "Forecast: Breakout Success"
    assert badge.kind == "forecast"
    assert "not a quality judgment" in badge.note


def test_failed_to_meet_expectations_is_provisional_and_never_named_a_flop():
    badge = status_badge(make_game(lifecycle_status=LifecycleStatus.FAILED_TO_MEET_EXPECTATIONS))

    assert badge.label == PROVISIONAL_LABEL
    assert badge.provisional is True
    assert "flop" not in badge.label.lower()


def test_unresolved_says_so_rather_than_guessing():
    badge = status_badge(make_game(lifecycle_status=LifecycleStatus.UNRESOLVED_INSUFFICIENT_DATA))

    assert badge.label == PROVISIONAL_LABEL
    assert badge.provisional is True
    assert "Insufficient public data" in badge.note


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (Outcome.FLOP, "Flop"),
        (Outcome.UNDERPERFORM, "Underperform"),
        (Outcome.SUCCESS, "Success"),
        (Outcome.BREAKOUT, "Breakout Success"),
    ],
)
def test_resolved_outcomes_get_their_plain_label(outcome, label):
    badge = status_badge(
        make_game(lifecycle_status=LifecycleStatus.RESOLVED, resolved_outcome=outcome)
    )

    assert badge.label == label
    assert badge.provisional is False
    assert badge.kind == "resolved"


def test_resolved_without_an_outcome_falls_back_to_provisional():
    badge = status_badge(make_game(lifecycle_status=LifecycleStatus.RESOLVED))

    assert badge.provisional is True
    assert badge.kind == "unresolved"


def test_outcomes_are_ordered_worst_to_best():
    ranks = [outcome.rank for outcome in Outcome]

    assert ranks == sorted(ranks)
    assert Outcome.FLOP.rank < Outcome.UNDERPERFORM.rank < Outcome.SUCCESS.rank
    assert Outcome.SUCCESS.rank < Outcome.BREAKOUT.rank


def test_failed_to_meet_expectations_is_not_a_trainable_outcome():
    # It is a lifecycle status, never a label the model learns to predict.
    assert "failed_to_meet_expectations" not in {outcome.value for outcome in Outcome}
