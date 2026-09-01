"""The parser must survive a response shape nobody has seen yet.

ITAD's history payload is not publicly documented and this was written without
a key, so `_observe` accepts several plausible spellings and raises with what it
actually saw when none fit. These fixtures pin the spellings it claims to
handle; the job's --dump-raw flag exists to settle the real one in one round.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.itad import (
    LAUNCH_WINDOW_DAYS,
    STEAM_SHOP_ID,
    ItadShapeError,
    LaunchPrice,
    earliest_regular_price,
)

# The shape the API most likely returns: a deal wrapper carrying both the
# charged price and the undiscounted regular price.
NESTED = [
    {
        "timestamp": "2024-03-01T00:00:00+00:00",
        "deal": {"price": {"amount": 29.99}, "regular": {"amount": 59.99}, "cut": 50},
    },
    {
        "timestamp": "2023-10-05T00:00:00+00:00",
        "deal": {"price": {"amount": 59.99}, "regular": {"amount": 59.99}, "cut": 0},
    },
]

FLAT = [
    {"date": "2023-10-05", "regular": 59.99, "price": 59.99, "cut": 0},
    {"date": "2024-03-01", "regular": 59.99, "price": 29.99, "cut": 50},
]


@pytest.mark.parametrize("history", [NESTED, FLAT])
def test_takes_the_oldest_record(history):
    observed = earliest_regular_price(history)

    assert observed is not None
    assert observed.recorded_on == date(2023, 10, 5)


@pytest.mark.parametrize("history", [NESTED, FLAT])
def test_reads_the_regular_price_not_the_sale_price(history):
    """A launch-week discount must not become the launch price."""
    assert earliest_regular_price(history).price_cents == 5999


def test_reads_the_regular_price_even_from_a_discounted_record():
    discounted_only = [
        {
            "timestamp": "2024-03-01T00:00:00Z",
            "deal": {"price": {"amount": 17.99}, "regular": {"amount": 59.99}, "cut": 70},
        }
    ]
    assert earliest_regular_price(discounted_only).price_cents == 5999


@pytest.mark.parametrize(
    "wrapped",
    [{"history": NESTED}, {"data": NESTED}, {"prices": NESTED}],
)
def test_unwraps_an_enveloped_payload(wrapped):
    assert earliest_regular_price(wrapped).price_cents == 5999


def test_empty_history_is_absence_not_an_error():
    assert earliest_regular_price([]) is None


def test_an_unrecognised_entry_names_what_it_saw():
    """The fix is to teach the parser one more spelling, so say which."""
    with pytest.raises(ItadShapeError, match="keys seen"):
        earliest_regular_price([{"when": "2023-01-01", "cost": 59.99}])


def test_a_non_list_payload_raises():
    with pytest.raises(ItadShapeError):
        earliest_regular_price("nope")


# --- is this actually a launch price? -------------------------------------


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (0, True),
        (LAUNCH_WINDOW_DAYS, True),
        (LAUNCH_WINDOW_DAYS + 1, False),
        (1500, False),  # ITAD started tracking a 2015 title in 2019
        (-1, True),  # a pre-order listing the day before release
        (-30, False),  # too far ahead to be the launch listing
        (None, False),  # no release date to measure against
    ],
)
def test_only_a_record_near_release_counts_as_the_launch_price(gap, expected):
    found = LaunchPrice(price_cents=5999, observed_on=date(2023, 10, 5), days_after_release=gap)

    assert found.is_launch_price is expected
    assert found.note


def test_steam_shop_id_is_pinned():
    """Verified against /service/shops/v1, which answers without a key."""
    assert STEAM_SHOP_ID == 61
