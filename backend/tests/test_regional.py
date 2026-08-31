"""Tests for regional reach — which markets can buy a game, and for how much.

The load-bearing rule here is that this is a **present-state** reading. Red
Dead Redemption 2 was on sale in Russia when it launched in 2019; its
unavailability dates from 2022. Anything that let this reach a historical row
would be the same trap as lifetime reviews and current price.
"""

from __future__ import annotations

import json
from datetime import timedelta

from app.ingest import REGIONAL_REFRESH_DAYS, ingest_game
from app.models import Game, utcnow
from app.steam import RegionalOffer, SteamError
from tests.conftest import RELEASED_APPID


class StubRegions:
    """Wraps a real stubbed client, overriding only the regional call."""

    def __init__(self, inner, offers=None, raises=False):
        self._inner = inner
        self._offers = offers or []
        self._raises = raises
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_regional_offers(self, appid, countries=None):
        self.calls += 1
        if self._raises:
            raise SteamError("transient")
        return self._offers


def test_a_closed_market_is_recorded_as_unavailable(session, steam_client):
    client = StubRegions(
        steam_client,
        [
            RegionalOffer("us", True, "USD", 5999),
            RegionalOffer("ru", False),
        ],
    )

    result = ingest_game(session, RELEASED_APPID, client)
    stored = json.loads(result.game.regional_offers)

    assert stored["us"] == {"available": True, "currency": "USD", "price_cents": 5999}
    assert stored["ru"] == {"available": False}
    assert result.game.regional_offers_at is not None


def test_a_transient_failure_does_not_break_the_ingest(session, steam_client):
    # Losing a nice-to-have sample must never cost the game's actual metadata.
    client = StubRegions(steam_client, raises=True)

    result = ingest_game(session, RELEASED_APPID, client)

    assert result.game.name
    assert result.game.regional_offers is None


def test_an_empty_result_is_not_written_as_an_absence(session, steam_client):
    client = StubRegions(steam_client, offers=[])

    result = ingest_game(session, RELEASED_APPID, client)

    assert result.game.regional_offers is None


def test_the_sample_is_not_refetched_on_every_poll(session, steam_client):
    client = StubRegions(steam_client, [RegionalOffer("us", True, "USD", 5999)])

    ingest_game(session, RELEASED_APPID, client)
    session.flush()
    ingest_game(session, RELEASED_APPID, client)

    assert client.calls == 1


def test_a_stale_sample_is_refreshed(session, steam_client):
    client = StubRegions(steam_client, [RegionalOffer("us", True, "USD", 5999)])
    result = ingest_game(session, RELEASED_APPID, client)
    session.flush()
    result.game.regional_offers_at = utcnow() - timedelta(days=REGIONAL_REFRESH_DAYS + 1)

    ingest_game(session, RELEASED_APPID, client)

    assert client.calls == 2


def test_regional_capture_can_be_skipped(session, steam_client):
    client = StubRegions(steam_client, [RegionalOffer("us", True, "USD", 5999)])

    ingest_game(session, RELEASED_APPID, client, with_regions=False)

    assert client.calls == 0


def test_historical_releases_have_no_regional_column():
    # The guard that matters: this is live data and must never reach a
    # historical row, where it would be anachronistic by years.
    from app.models import HistoricalRelease

    assert not hasattr(HistoricalRelease, "regional_offers")
    assert hasattr(Game, "regional_offers")
