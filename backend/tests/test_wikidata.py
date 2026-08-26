"""Tests for the Wikidata client, against a recorded response — no network.

The interesting cases are all about refusing to invent a date. A wrong
`original_release_date` doesn't fail loudly; it silently reclassifies a launch
as a port (or the reverse), which then excludes or admits the wrong rows for
labeling.
"""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from app.wikidata import ReleaseDates, WikidataClient, WikidataError


def response(pairs: list[tuple[str, str]]):
    """Build a SPARQL JSON payload the way the endpoint returns one."""
    payload = {
        "results": {
            "bindings": [
                {"appid": {"value": appid}, "date": {"value": value}} for appid, value in pairs
            ]
        }
    }
    body = json.dumps(payload).encode()

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        return _Response(body)

    return opener


def client(opener) -> WikidataClient:
    return WikidataClient(opener=opener, delay_seconds=0)


def test_the_earliest_date_across_platforms_is_the_original_release():
    # Red Dead Redemption 2: console 2018, Windows 2019, Steam later still.
    opener = response(
        [
            ("1174180", "2018-10-26T00:00:00Z"),
            ("1174180", "2019-11-05T00:00:00Z"),
            ("1174180", "2019-12-05T00:00:00Z"),
        ]
    )
    found = client(opener).release_dates([1174180])

    assert found[1174180].earliest == date(2018, 10, 26)
    assert len(found[1174180].dates) == 3


def test_a_year_precision_date_is_dropped_not_coerced():
    # Wikidata renders year-only precision as 2016-00-00, which is not a date.
    # Coercing it to January 1st could turn a same-year launch into a port.
    opener = response([("1", "2016-00-00T00:00:00Z"), ("1", "2016-06-15T00:00:00Z")])
    found = client(opener).release_dates([1])

    assert found[1].dates == [date(2016, 6, 15)]


def test_an_appid_with_only_an_unusable_date_is_absent_entirely():
    opener = response([("1", "2016-00-00T00:00:00Z")])
    assert client(opener).release_dates([1]) == {}


def test_an_unmatched_appid_is_simply_missing_rather_than_an_error():
    # Not every game has a Wikidata item carrying P1733. That is ordinary.
    opener = response([("1", "2020-01-01T00:00:00Z")])
    found = client(opener).release_dates([1, 999])

    assert 1 in found
    assert 999 not in found


def test_a_malformed_appid_in_the_response_is_skipped():
    opener = response([("not-a-number", "2020-01-01T00:00:00Z"), ("2", "2021-02-02T00:00:00Z")])
    found = client(opener).release_dates([2])

    assert list(found) == [2]


def test_a_network_failure_raises_rather_than_returning_nothing():
    # Returning {} on failure would look identical to "nothing matched", and
    # the job would report full coverage of an empty result.
    def opener(request, timeout=None):
        raise TimeoutError("endpoint timed out")

    with pytest.raises(WikidataError, match="Wikidata query failed"):
        client(opener).release_dates([1])


def test_lookups_are_batched():
    seen: list[str] = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        return response([])(request, timeout)

    WikidataClient(opener=opener, batch_size=2, delay_seconds=0).release_dates([1, 2, 3, 4, 5])

    assert len(seen) == 3


def test_release_dates_with_nothing_recorded_has_no_earliest():
    assert ReleaseDates(steam_appid=1, dates=[]).earliest is None


def test_the_client_identifies_itself_to_wikimedia():
    # Wikimedia asks automated clients to send a descriptive User-Agent with a
    # contact route; an anonymous agent risks being blocked outright.
    seen: list[str] = []

    def opener(request, timeout=None):
        seen.append(request.get_header("User-agent"))
        return response([])(request, timeout)

    client(opener).release_dates([1])

    assert "aaa-launch-predictor" in seen[0]
    assert "github.com" in seen[0]
