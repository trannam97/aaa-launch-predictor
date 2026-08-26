"""Tests for the Steam client and its parsing."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.steam import (
    SteamAppNotFound,
    SteamClient,
    SteamError,
    SteamUnavailable,
    parse_release_date,
)
from tests.conftest import (
    MISSING_APPID,
    RELEASED_APPID,
    UPCOMING_APPID,
    load_fixture,
    steam_transport,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Oct 26, 2018", date(2018, 10, 26)),
        ("26 Oct, 2018", date(2018, 10, 26)),
        ("October 26, 2018", date(2018, 10, 26)),
        ("Nov 2026", date(2026, 11, 1)),
        ("2026", date(2026, 1, 1)),
        ("Q1 2027", date(2027, 1, 1)),
        ("Q4 2026", date(2026, 10, 1)),
        ("Coming soon", None),
        ("To be announced", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_release_date(raw, expected):
    assert parse_release_date(raw) == expected


def test_get_app_details_parses_released_game(steam_client: SteamClient):
    details = steam_client.get_app_details(RELEASED_APPID)

    assert details.name == "Example Released Game"
    assert details.developers == ["Example Studios"]
    assert details.publishers == ["Example Interactive"]
    assert details.genres == ["Action", "Adventure"]
    assert details.release_date == date(2018, 10, 26)
    assert details.release_date_raw == "Oct 26, 2018"
    assert details.coming_soon is False
    assert details.price_initial_cents == 5999
    assert details.price_final_cents == 2999
    assert details.discount_percent == 50
    assert details.price_currency == "USD"
    assert (details.on_windows, details.on_mac, details.on_linux) == (True, False, False)
    assert details.metacritic_score == 86


def test_get_app_details_handles_upcoming_game_without_price(steam_client_factory):
    client = steam_client_factory(details=load_fixture("appdetails_upcoming.json"))

    details = client.get_app_details(UPCOMING_APPID)

    assert details.coming_soon is True
    assert details.release_date == date(2026, 10, 1)
    assert details.release_date_raw == "Q4 2026"
    assert details.price_initial_cents is None
    assert details.metacritic_score is None
    assert details.developers == ["Example Studios", "Example Support Studio"]


def test_get_app_details_raises_for_unknown_app(steam_client_factory):
    client = steam_client_factory(details=load_fixture("appdetails_missing.json"))

    with pytest.raises(SteamAppNotFound):
        client.get_app_details(MISSING_APPID)


def test_get_review_summary(steam_client: SteamClient):
    reviews = steam_client.get_review_summary(RELEASED_APPID)

    assert reviews.total == 96257
    assert reviews.positive == 84213
    assert reviews.negative == 12044
    assert reviews.score_desc == "Very Positive"


def test_get_review_summary_for_game_with_no_reviews(steam_client_factory):
    client = steam_client_factory(reviews=load_fixture("appreviews_empty.json"))

    reviews = client.get_review_summary(UPCOMING_APPID)

    assert reviews.total == 0
    assert reviews.score_desc == "No user reviews"


def test_get_current_players(steam_client: SteamClient):
    assert steam_client.get_current_players(RELEASED_APPID) == 18342


def test_get_current_players_returns_none_when_steam_declines(steam_client_factory):
    client = steam_client_factory(players={"response": {"result": 42}})

    assert client.get_current_players(UPCOMING_APPID) is None


def test_rate_limit_raises_unavailable(steam_client_factory):
    client = steam_client_factory(status_code=429)

    with pytest.raises(SteamUnavailable):
        client.get_app_details(RELEASED_APPID)


def test_server_error_raises_unavailable(steam_client_factory):
    client = steam_client_factory(status_code=503)

    with pytest.raises(SteamUnavailable):
        client.get_app_details(RELEASED_APPID)


def test_client_error_raises_steam_error(steam_client_factory):
    client = steam_client_factory(status_code=403)

    with pytest.raises(SteamError):
        client.get_app_details(RELEASED_APPID)


def test_network_failure_raises_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with httpx.Client(transport=httpx.MockTransport(boom)) as http_client:
        client = SteamClient(http_client, min_request_interval=0)
        with pytest.raises(SteamUnavailable):
            client.get_app_details(RELEASED_APPID)


def test_details_request_pins_country_and_language():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=load_fixture("appdetails_released.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        SteamClient(http_client, min_request_interval=0).get_app_details(RELEASED_APPID)

    assert seen["cc"] == "us"
    assert seen["l"] == "english"
    assert seen["appids"] == str(RELEASED_APPID)


def test_unused_transport_helper_rejects_unknown_paths():
    transport = steam_transport()
    with httpx.Client(transport=transport) as http_client, pytest.raises(AssertionError):
        http_client.get("https://store.steampowered.com/api/unknown")


def test_client_paces_requests_to_avoid_rate_limits():
    """A burst of requests must be spaced, not fired all at once.

    Dating a title's DLC can issue twenty requests on top of the four every
    game already costs, which is how Steam's limit got tripped in practice.
    """
    import time

    calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        return httpx.Response(200, json=load_fixture("appdetails_released.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(http_client, min_request_interval=0.05)
        for _ in range(3):
            client.get_app_details(RELEASED_APPID)

    # Deliberately offset by one, so the lists differ in length.
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert all(gap >= 0.045 for gap in gaps), gaps


def test_throttle_can_be_disabled_for_tests():
    with httpx.Client(transport=steam_transport()) as http_client:
        client = SteamClient(http_client, min_request_interval=0)
        client.get_app_details(RELEASED_APPID)
        client.get_app_details(RELEASED_APPID)
