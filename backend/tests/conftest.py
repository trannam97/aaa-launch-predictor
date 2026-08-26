"""Shared test fixtures.

Every test runs against an in-memory SQLite database and a stubbed Steam
transport — no network, no local DB file. The stub replays the recorded
payloads in tests/fixtures/, which are trimmed copies of real Steam
responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.steam import SteamClient

FIXTURE_DIR = Path(__file__).parent / "fixtures"

RELEASED_APPID = 1174180
UPCOMING_APPID = 2000950
MISSING_APPID = 999999999


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.fixture
def engine():
    """One in-memory SQLite database, shared across connections in a test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def steam_transport(
    *,
    details: dict | None = None,
    reviews: dict | None = None,
    players: dict | None = None,
    status_code: int = 200,
) -> httpx.MockTransport:
    """Build a transport that answers the three Steam endpoints we call."""
    details = details if details is not None else load_fixture("appdetails_released.json")
    reviews = reviews if reviews is not None else load_fixture("appreviews_released.json")
    players = players if players is not None else load_fixture("currentplayers.json")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/appdetails"):
            return httpx.Response(status_code, json=details)
        if "/appreviews/" in path:
            return httpx.Response(status_code, json=reviews)
        if "GetNumberOfCurrentPlayers" in path:
            return httpx.Response(status_code, json=players)
        raise AssertionError(f"unexpected Steam request: {request.url}")

    return httpx.MockTransport(handler)


@pytest.fixture
def steam_client_factory():
    """Factory returning a SteamClient wired to a stubbed transport."""

    http_clients: list[httpx.Client] = []

    def make(**kwargs) -> SteamClient:
        # SteamClient only closes transports it created itself, so the
        # fixture holds onto the injected one and closes it at teardown.
        http_client = httpx.Client(transport=steam_transport(**kwargs))
        http_clients.append(http_client)
        # No throttling against a stubbed transport — there is nothing to
        # rate-limit, and sleeping would make the suite crawl.
        return SteamClient(http_client, min_request_interval=0)

    yield make
    for http_client in http_clients:
        http_client.close()


@pytest.fixture
def steam_client(steam_client_factory) -> SteamClient:
    return steam_client_factory()
