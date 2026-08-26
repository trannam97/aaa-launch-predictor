"""Tests for the pre-launch feature contract.

The interesting assertions here are the negative ones. Every field in
`FORBIDDEN_FIELDS` would raise the model's measured accuracy while
destroying its actual purpose, and that failure is invisible in a score, so
the guard against it needs a test that fails loudly when someone removes it.
"""

from __future__ import annotations

import pytest

from app.features import FEATURE_NAMES, FORBIDDEN_FIELDS, LeakageError, assert_no_leakage


def test_current_feature_set_is_clean():
    assert_no_leakage()


@pytest.mark.parametrize(
    "field",
    ["positive_pct", "review_total", "retention_ratio", "studio_signal", "metacritic_score"],
)
def test_a_post_launch_field_is_rejected(field: str):
    with pytest.raises(LeakageError, match=field):
        assert_no_leakage((*FEATURE_NAMES, field))


def test_the_error_names_every_leaked_field_not_just_the_first():
    with pytest.raises(LeakageError) as caught:
        assert_no_leakage(("positive_pct", "launch_price_usd", "retention_ratio"))
    message = str(caught.value)
    assert "positive_pct" in message
    assert "retention_ratio" in message
    assert "launch_price_usd" not in message


def test_launch_window_review_fields_are_all_forbidden():
    # These are the ones that would flatter the model most: they are close to
    # a restatement of the label the rubric derives from the same numbers.
    for field in ("review_total", "review_positive", "positive_pct", "volume_percentile"):
        assert field in FORBIDDEN_FIELDS


def test_no_feature_name_is_repeated():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
