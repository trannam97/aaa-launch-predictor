"""The parser must survive a response shape nobody has seen yet.

ITAD's history payload is not publicly documented and this was written without
a key, so `_observe` accepts several plausible spellings and raises with what it
actually saw when none fit. These fixtures pin the spellings it claims to
handle; the job's --dump-raw flag exists to settle the real one in one round.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.itad import (
    LAUNCH_WINDOW_DAYS,
    LOOKUP_BATCH_SIZE,
    MAX_RETRY_WAIT_SECONDS,
    RATE_LIMIT_ATTEMPTS,
    STEAM_SHOP_ID,
    ItadAuthError,
    ItadClient,
    ItadError,
    ItadRateLimitError,
    ItadShapeError,
    LaunchPrice,
    _explain_403,
    _retry_after_seconds,
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
        # The margin `history` asks from. Every row of the first working run
        # landed here and was wrongly discarded.
        (-25, True),
        (-30, True),
        (-31, False),  # earlier than we asked for, so unexplained
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


def test_a_pre_order_listing_is_the_launch_price():
    """The record the `since` margin exists to fetch, and it must count.

    Watch_Dogs came back at $59.99 observed 30 days before release — correct,
    and its actual 2014 launch price. A bound of -1 discarded all twenty rows
    of the first working run for being exactly what was asked for.
    """
    found = LaunchPrice(
        price_cents=5999,
        observed_on=date(2014, 4, 26),
        days_after_release=-30,
        coverage="parsed 5 of 5 history entries, spanning 2014-04-26 to 2014-06-01",
    )

    assert found.is_launch_price
    assert "pre-order listing, 30d before release" in found.note
    assert "not necessarily" not in found.note


# --- the allowance runs out, and that is not 120 separate failures ----------


class RateLimited:
    """An API that 429s, optionally with a Retry-After, then relents."""

    def __init__(self, refusals: int, retry_after: str | None = None) -> None:
        self.refusals = refusals
        self.retry_after = retry_after
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        if self.calls <= self.refusals:
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(429, headers=headers, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"found": False}, request=httpx.Request("GET", url))


def test_a_429_is_retried_before_it_is_believed(monkeypatch):
    """A short window can be outlasted, so one refusal is not the answer."""
    slept: list[float] = []
    monkeypatch.setattr("app.itad.time.sleep", slept.append)
    api = RateLimited(refusals=1, retry_after="2")

    client = ItadClient("k", client=api, min_request_interval=0)
    assert client._get("/games/history/v2", {}) == {"found": False}
    assert slept == [2.0]


def test_a_persistent_429_stops_the_run_instead_of_burning_the_queue(monkeypatch):
    """The failure this class exists for: 120 games asked after the budget went."""
    monkeypatch.setattr("app.itad.time.sleep", lambda _: None)
    api = RateLimited(refusals=99)

    with pytest.raises(ItadRateLimitError, match="allowance is spent"):
        ItadClient("k", client=api, min_request_interval=0)._get("/games/history/v2", {})

    assert api.calls == RATE_LIMIT_ATTEMPTS


def test_a_rate_limit_error_is_not_mistaken_for_a_per_row_failure():
    """Callers catch ItadError per row; these two must escape that."""
    assert issubclass(ItadRateLimitError, ItadError)
    assert not issubclass(ItadRateLimitError, ItadAuthError)


def test_a_long_retry_after_is_refused_rather_than_slept_through(monkeypatch):
    """An hour-long wait is a stalled runner, not a retry."""
    slept: list[float] = []
    monkeypatch.setattr("app.itad.time.sleep", slept.append)
    api = RateLimited(refusals=99, retry_after=str(int(MAX_RETRY_WAIT_SECONDS) + 1))

    with pytest.raises(ItadRateLimitError) as raised:
        ItadClient("k", client=api, min_request_interval=0)._get("/games/history/v2", {})

    assert slept == []
    assert api.calls == 1
    assert raised.value.retry_after == MAX_RETRY_WAIT_SECONDS + 1


def test_retry_after_reads_both_legal_spellings():
    """RFC 9110 allows a delay in seconds or an HTTP date."""
    seconds = httpx.Response(429, headers={"Retry-After": "30"})
    assert _retry_after_seconds(seconds) == 30.0

    when = (datetime.now(UTC) + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    as_date = httpx.Response(429, headers={"Retry-After": when})
    assert 30 < _retry_after_seconds(as_date) <= 46

    assert _retry_after_seconds(httpx.Response(429)) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None


def test_the_rate_limit_message_says_how_to_recover(monkeypatch):
    """It is resumable, and the limit is visible — say both, not "HTTP 429"."""
    monkeypatch.setattr("app.itad.time.sleep", lambda _: None)
    api = RateLimited(refusals=99)
    with pytest.raises(ItadRateLimitError) as raised:
        ItadClient("k", client=api, min_request_interval=0)._get("/x", {})

    assert "Re-run" in str(raised.value)
    assert "isthereanydeal.com/apps/dev" in str(raised.value)


# --- one request for the whole queue, not one per game ----------------------


class BulkLookup:
    def __init__(self, body: object, status: int = 200) -> None:
        self.body, self.status = body, status
        self.seen: dict[str, object] = {}
        self.batches: list[list[str]] = []

    def post(self, url, json=None):
        self.seen["url"], self.seen["json"] = url, json
        self.batches.append(list(json or []))
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


def test_the_whole_queue_is_resolved_in_one_request():
    """170 games cost 170 lookups before this, which is half of why 429 hit."""
    api = BulkLookup({"app/570": "id-570", "app/440": "id-440"})

    found = ItadClient("k", client=api, min_request_interval=0).lookup_many([570, 440])

    assert found == {570: "id-570", 440: "id-440"}
    assert api.seen["url"].endswith(f"/lookup/id/shop/{STEAM_SHOP_ID}/v1")
    assert api.seen["json"] == ["app/440", "app/570"]


def test_the_bulk_lookup_sends_no_api_key():
    """Verified keyless against the live API, and a keyless request cannot
    spend an allowance that is counted per app."""
    api = BulkLookup({})

    ItadClient("secret", client=api, min_request_interval=0).lookup_many([570])

    assert "secret" not in str(api.seen)


def test_an_appid_itad_does_not_know_is_simply_absent():
    api = BulkLookup({"app/570": "id-570"})

    found = ItadClient("k", client=api, min_request_interval=0).lookup_many([570, 999999])

    assert found == {570: "id-570"}


def test_looking_up_nothing_makes_no_request():
    api = BulkLookup({})

    assert ItadClient("k", client=api, min_request_interval=0).lookup_many([]) == {}
    assert api.seen == {}


def test_a_single_lookup_goes_through_the_same_keyless_endpoint():
    api = BulkLookup({"app/570": "id-570"})
    client = ItadClient("k", client=api, min_request_interval=0)

    assert client.lookup(570) == "id-570"
    assert client.lookup(440) is None


def test_the_throttle_spaces_requests_out(monkeypatch):
    """Unthrottled, the first run spent the whole allowance in eighteen seconds."""
    slept: list[float] = []
    monkeypatch.setattr("app.itad.time.sleep", slept.append)
    monkeypatch.setattr("app.itad.time.monotonic", lambda: 100.0)
    api = RateLimited(refusals=0)

    client = ItadClient("k", client=api, min_request_interval=1.0)
    client._get("/x", {})
    client._get("/x", {})

    assert slept == [1.0]  # the second request waited out the interval


def test_a_queue_larger_than_one_batch_is_split():
    """206 in one POST is verified; a corpus grown past 300 must not discover
    an undocumented ceiling as a blanket failure."""
    api = BulkLookup({})
    appids = list(range(1, LOOKUP_BATCH_SIZE * 2 + 3))

    ItadClient("k", client=api, min_request_interval=0).lookup_many(appids)

    assert [len(batch) for batch in api.batches] == [LOOKUP_BATCH_SIZE, LOOKUP_BATCH_SIZE, 2]
