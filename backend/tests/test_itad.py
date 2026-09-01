"""The parser must survive a response shape nobody has seen yet.

ITAD's history payload is not publicly documented and this was written without
a key, so `_observe` accepts several plausible spellings and raises with what it
actually saw when none fit. These fixtures pin the spellings it claims to
handle; the job's --dump-raw flag exists to settle the real one in one round.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.itad import (
    LAUNCH_WINDOW_DAYS,
    STEAM_SHOP_ID,
    ItadClient,
    ItadError,
    ItadShapeError,
    LaunchPrice,
    _explain_403,
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

    assert observed.observation is not None
    assert observed.observation.recorded_on == date(2023, 10, 5)


@pytest.mark.parametrize("history", [NESTED, FLAT])
def test_reads_the_regular_price_not_the_sale_price(history):
    """A launch-week discount must not become the launch price."""
    assert earliest_regular_price(history).observation.price_cents == 5999


def test_reads_the_regular_price_even_from_a_discounted_record():
    discounted_only = [
        {
            "timestamp": "2024-03-01T00:00:00Z",
            "deal": {"price": {"amount": 17.99}, "regular": {"amount": 59.99}, "cut": 70},
        }
    ]
    assert earliest_regular_price(discounted_only).observation.price_cents == 5999


@pytest.mark.parametrize(
    "wrapped",
    [{"history": NESTED}, {"data": NESTED}, {"prices": NESTED}],
)
def test_unwraps_an_enveloped_payload(wrapped):
    assert earliest_regular_price(wrapped).observation.price_cents == 5999


def test_empty_history_is_absence_not_an_error():
    read = earliest_regular_price([])

    assert read.observation is None
    assert read.total == 0


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


# --- what a 403 actually was -----------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"status_code":403,"reason_phrase":"Missing api key"}', "no api key"),
        ('{"status_code":403,"reason_phrase":"Invalid or expired api key"}', "invalid or expired"),
        ("error code: 1010", "Cloudflare"),
        ("something unexpected", "rejected:"),
        ("", "rejected:"),
    ],
)
def test_a_403_says_which_of_three_faults_it_was(body, expected):
    """All three arrive as 403 and need different fixes, so never collapse them."""
    assert expected in _explain_403(body)


def test_the_missing_key_message_names_where_the_key_goes():
    """Header auth is silently ignored by ITAD and reads as missing."""
    explained = _explain_403('{"reason_phrase":"Missing api key"}')

    assert "`key` query parameter" in explained
    assert "Authorization" in explained


def test_a_pasted_newline_is_stripped_from_the_key():
    """A secret copied with a trailing newline reaches ITAD as %0A."""
    assert ItadClient("abc123\n", client=object())._key == "abc123"
    assert ItadClient("  abc123  ", client=object())._key == "abc123"


# --- partial parsing must never look like a complete answer -----------------


def test_a_partly_understood_payload_is_flagged():
    """The failure this parser is most likely to have, made visible.

    One entry in a shape `_observe` knows, beside entries it does not, used to
    return that one entry as "the oldest record" with nothing to say otherwise.
    A single current price is indistinguishable from a real history that way.
    """
    mixed = [
        {"timestamp": "2026-09-01T00:00:00Z", "deal": {"regular": {"amount": 19.99}, "cut": 0}},
        {"unrecognised": "shape"},
        {"another": "unknown"},
    ]
    read = earliest_regular_price(mixed)

    assert read.parsed == 1
    assert read.total == 3
    assert read.is_suspect
    assert "parsed 1 of 3" in read.coverage


def test_a_fully_understood_payload_is_not_flagged():
    read = earliest_regular_price(NESTED)

    assert read.parsed == read.total == 2
    assert not read.is_suspect


def test_a_suspect_read_is_never_called_a_launch_price():
    """Date arithmetic on a partial payload is meaningless, so refuse it."""
    found = LaunchPrice(
        price_cents=1999,
        observed_on=date(2026, 9, 1),
        days_after_release=0,  # would otherwise pass the window cleanly
        coverage="parsed 1 of 340 history entries",
        suspect_shape=True,
    )

    assert not found.is_launch_price
    assert "UNRELIABLE" in found.note
    assert "parsed 1 of 340" in found.note


# --- reaching past the default window ---------------------------------------


def test_history_asks_from_before_release():
    """Without `since`, ITAD answers with a recent window only.

    A captured response held five change events across eleven weeks, every one
    carrying the title's present regular price rather than anything it launched
    at. `since` is the difference between reading history and reading today.
    """
    seen: dict[str, object] = {}

    class Recorder:
        def get(self, url, params=None):
            seen["url"], seen["params"] = url, params
            raise httpx.HTTPError("stop here — the request is what is under test")

    client = ItadClient("k", client=Recorder())
    with pytest.raises(ItadError):
        client.history("abc", since=date(2014, 4, 26))

    assert seen["params"]["since"].startswith("2014-04-26")
    assert seen["params"]["shops"] == STEAM_SHOP_ID
    assert seen["params"]["country"] == "US"


def test_history_omits_since_when_there_is_no_release_date():
    seen: dict[str, object] = {}

    class Recorder:
        def get(self, url, params=None):
            seen["params"] = params
            raise httpx.HTTPError("stop")

    with pytest.raises(ItadError):
        ItadClient("k", client=Recorder()).history("abc")

    assert "since" not in seen["params"]


def test_coverage_reports_the_span_actually_returned():
    """The evidence that `since` was honoured, or silently ignored."""
    read = earliest_regular_price(NESTED)

    assert "spanning 2023-10-05 to 2024-03-01" in read.coverage
