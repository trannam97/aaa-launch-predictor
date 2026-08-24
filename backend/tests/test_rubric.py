"""Tests for the cohort index and the outcome rubric."""

from __future__ import annotations

import pytest

from app.cohort import MIN_COHORT_SIZE, CohortIndex, CohortStats, PriceIndex
from app.models import Outcome, StudioSignal, SupportSignal
from app.rubric import RubricInput, classify

# --- cohort ---------------------------------------------------------------


def test_percentile_ranks_within_cohort():
    stats = CohortStats(year=2020, values=[10, 20, 30, 40, 50, 60, 70, 80])

    assert stats.percentile_of(5) == 0.0
    assert stats.percentile_of(100) == 100.0
    assert stats.percentile_of(45) == 50.0


def test_percentile_splits_ties():
    # A game that merely matched its peers should not be credited with
    # beating them.
    stats = CohortStats(year=2020, values=[10, 10, 10, 10])

    assert stats.percentile_of(10) == 50.0


def test_cohort_pools_neighbouring_years():
    index = CohortIndex({2019: [1, 2, 3], 2020: [4, 5, 6], 2021: [7, 8, 9]})

    assert index.stats_for(2020).size == 9
    assert index.stats_for(2019).size == 6  # no 2018 data to pool


def test_small_cohort_returns_no_percentile():
    # An unreliable percentile is worse than none: everything downstream
    # would treat it as real.
    index = CohortIndex({2020: [1, 2, 3]})

    percentile, stats = index.percentile(2020, 2)

    assert percentile is None
    assert stats.is_reliable is False
    assert stats.size < MIN_COHORT_SIZE


def test_reliable_cohort_returns_a_percentile():
    index = CohortIndex({2020: list(range(1, 21))})

    percentile, stats = index.percentile(2020, 10)

    assert stats.is_reliable is True
    assert percentile is not None


# --- rubric ---------------------------------------------------------------


def signals(**kwargs) -> RubricInput:
    base = {
        "volume_percentile": 60.0,
        "positive_pct": 85.0,
        "retention_ratio": 1.5,
        "studio_signal": StudioSignal.CONTINUED,
        "support_signal": SupportSignal.SUSTAINED,
    }
    base.update(kwargs)
    return RubricInput(**base)


def test_strong_sentiment_and_volume_is_a_success():
    result = classify(signals())

    assert result.outcome is Outcome.SUCCESS
    assert result.reasons


def test_top_cohort_volume_is_a_breakout():
    result = classify(signals(volume_percentile=95.0, positive_pct=93.0))

    assert result.outcome is Outcome.BREAKOUT


def test_high_volume_with_compounding_reviews_is_a_breakout():
    # Space Marine 2's shape: strong but not top-percentile launch volume that
    # kept accruing reviews.
    result = classify(signals(volume_percentile=81.0, positive_pct=82.0, retention_ratio=2.0))

    assert result.outcome is Outcome.BREAKOUT


def test_poor_sentiment_misses_however_big_the_launch():
    # Battlefield 2042: 89th-percentile volume, 34% positive. Volume measures
    # attention, not success.
    result = classify(signals(volume_percentile=89.0, positive_pct=34.0))

    assert result.outcome is Outcome.UNDERPERFORM
    assert any("below the 78% bar" in reason for reason in result.reasons)


def test_good_reviews_with_no_audience_misses():
    # Hellblade II: 88% positive on 18th-percentile volume.
    result = classify(signals(volume_percentile=18.0, positive_pct=88.0))

    assert result.outcome is Outcome.UNDERPERFORM
    assert any("floor" in reason for reason in result.reasons)


def test_momentum_rescues_a_rocky_launch():
    # Helldivers 2: 75% positive through server failures, then nearly 6x its
    # launch reviews by three months.
    result = classify(signals(volume_percentile=93.0, positive_pct=75.0, retention_ratio=5.9))

    assert result.outcome is Outcome.BREAKOUT
    assert any("kept growing" in reason for reason in result.reasons)


def test_studio_closure_is_a_flop():
    result = classify(
        signals(
            positive_pct=60.0,
            studio_signal=StudioSignal.CLOSED,
            support_signal=SupportSignal.ABANDONED,
        )
    )

    assert result.outcome is Outcome.FLOP
    assert result.confidence == "high"


def test_abandoned_game_is_a_flop_even_if_the_studio_lives():
    # Marvel's Avengers: Crystal Dynamics survived, the game was delisted.
    result = classify(
        signals(
            positive_pct=66.0,
            studio_signal=StudioSignal.CONTINUED,
            support_signal=SupportSignal.ABANDONED,
        )
    )

    assert result.outcome is Outcome.FLOP


def test_layoffs_with_delivered_support_is_underperform():
    # The Callisto Protocol: gutted studio, but the season pass shipped.
    result = classify(
        signals(
            positive_pct=59.0,
            studio_signal=StudioSignal.SEVERE_LAYOFFS,
            support_signal=SupportSignal.SUSTAINED,
        )
    )

    assert result.outcome is Outcome.UNDERPERFORM


def test_layoffs_with_curtailed_support_is_a_flop():
    result = classify(
        signals(
            positive_pct=70.0,
            studio_signal=StudioSignal.SEVERE_LAYOFFS,
            support_signal=SupportSignal.CURTAILED,
        )
    )

    assert result.outcome is Outcome.FLOP


def test_shortfall_with_no_post_launch_evidence_stays_unresolved():
    # This is the "Failed to Meet Expectations" state: clearly short, but
    # nothing yet says how short.
    result = classify(
        signals(
            positive_pct=50.0,
            studio_signal=StudioSignal.UNKNOWN,
            support_signal=SupportSignal.UNKNOWN,
        )
    )

    assert result.outcome is None
    assert result.resolved is False
    assert "separate Flop from Underperform" in result.unresolved_reason


def test_missing_volume_refuses_to_classify():
    result = classify(signals(volume_percentile=None))

    assert result.outcome is None
    assert result.unresolved_reason is not None


def test_unreliable_cohort_says_so():
    result = classify(signals(volume_percentile=None, cohort_reliable=False))

    assert result.outcome is None
    assert "cohort too small" in result.unresolved_reason


@pytest.mark.parametrize("support", [SupportSignal.UNKNOWN, SupportSignal.ABANDONED])
def test_upper_tier_confidence_drops_without_a_settled_support_picture(support):
    result = classify(signals(volume_percentile=95.0, positive_pct=93.0, support_signal=support))

    assert result.outcome is Outcome.BREAKOUT
    assert result.confidence == "low"


# --- price normalization --------------------------------------------------


def test_going_rate_is_the_modal_price_not_the_mean():
    # AAA pricing clusters on a headline number. A mean would report $63.75,
    # which nobody ever charged.
    index = PriceIndex({2023: [7000] * 6 + [6000, 4000]})

    assert index.going_rate(2023) == 7000


def test_going_rate_breaks_ties_toward_the_higher_price():
    # In a step year the new rate should win, not the outgoing one.
    index = PriceIndex({2023: [6000] * 4 + [7000] * 4})

    assert index.going_rate(2023) == 7000


def test_relative_price_expresses_the_going_rate_as_1():
    index = PriceIndex({2024: [7000] * 10})

    assert index.relative_price(2024, 7000) == 1.0
    assert index.relative_price(2024, 4000) == 0.571
    assert index.relative_price(2024, 8000) == 1.143


def test_same_nominal_price_reads_differently_across_eras():
    # The point of the whole exercise: $60 was the going rate in 2016 and
    # below it in 2024, so the same number must not mean the same thing.
    index = PriceIndex({2016: [6000] * 10, 2024: [7000] * 10})

    assert index.relative_price(2016, 6000) == 1.0
    assert index.relative_price(2024, 6000) == 0.857


def test_thin_price_cohort_returns_none():
    # Only curated rows carry a launch price, so early cohorts are sparse.
    # An invented going rate would be worse than no answer.
    index = PriceIndex({2015: [6000, 6000]})

    assert index.going_rate(2015) is None
    assert index.relative_price(2015, 6000) is None


def test_free_and_missing_prices_are_not_ranked():
    index = PriceIndex({2024: [7000] * 10})

    assert index.relative_price(2024, None) is None
    assert index.relative_price(2024, 0) is None
    assert index.relative_price(None, 7000) is None
