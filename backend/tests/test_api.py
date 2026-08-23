"""End-to-end tests for the Phase 0 API, against SQLite and a stubbed Steam."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import app, get_steam_client
from app.models import Game, LifecycleStatus, Outcome
from tests.conftest import MISSING_APPID, RELEASED_APPID, UPCOMING_APPID, load_fixture


@pytest.fixture
def client_factory(engine, steam_client_factory):
    """Build a TestClient whose DB and Steam access are both stubbed."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def make(**steam_kwargs) -> TestClient:
        def override_db():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        steam = steam_client_factory(**steam_kwargs)
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_steam_client] = lambda: steam
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()


def test_health(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "Not affiliated with Valve" in body["disclaimer"]


def test_track_game_ingests_and_returns_detail(client: TestClient):
    response = client.post("/games", json={"steam_appid": RELEASED_APPID})

    assert response.status_code == 201
    body = response.json()
    assert body["steam_appid"] == RELEASED_APPID
    assert body["name"] == "Example Released Game"
    assert body["genres"] == ["Action", "Adventure"]
    assert body["release_date"] == "2018-10-26"
    assert body["metacritic_score"] == 86
    assert body["latest_snapshot"]["review_total"] == 96257
    assert body["latest_snapshot"]["positive_pct"] == 87.5
    assert body["latest_snapshot"]["concurrent_players"] == 18342
    assert len(body["snapshots"]) == 1


def test_tracking_the_same_game_twice_refreshes_instead_of_duplicating(client: TestClient):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    response = client.post("/games", json={"steam_appid": RELEASED_APPID})

    assert response.status_code == 200
    assert len(client.get("/games").json()) == 1


def test_track_unknown_app_returns_404(client_factory):
    client = client_factory(details=load_fixture("appdetails_missing.json"))

    response = client.post("/games", json={"steam_appid": MISSING_APPID})

    assert response.status_code == 404
    assert len(client.get("/games").json()) == 0


def test_track_returns_503_when_steam_is_rate_limiting(client_factory):
    client = client_factory(status_code=429)

    response = client.post("/games", json={"steam_appid": RELEASED_APPID})

    assert response.status_code == 503


def test_list_games_orders_by_release_date(client_factory):
    released = client_factory()
    released.post("/games", json={"steam_appid": RELEASED_APPID})
    upcoming = client_factory(
        details=load_fixture("appdetails_upcoming.json"),
        reviews=load_fixture("appreviews_empty.json"),
    )
    upcoming.post("/games", json={"steam_appid": UPCOMING_APPID})

    body = released.get("/games").json()

    assert [game["steam_appid"] for game in body] == [RELEASED_APPID, UPCOMING_APPID]
    assert body[0]["status"]["kind"] == "tracking"
    assert body[1]["status"]["kind"] == "forecast"


def test_list_games_reports_the_newest_snapshot(client_factory):
    client = client_factory()
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    updated = load_fixture("appreviews_released.json")
    updated["query_summary"]["total_reviews"] = 111000
    refreshed = client_factory(reviews=updated)
    refreshed.post(f"/games/{RELEASED_APPID}/refresh")

    [game] = refreshed.get("/games").json()

    assert game["latest_snapshot"]["review_total"] == 111000


def test_get_game_detail_includes_snapshot_history(client: TestClient):
    client.post("/games", json={"steam_appid": RELEASED_APPID})
    client.post(f"/games/{RELEASED_APPID}/refresh")

    body = client.get(f"/games/{RELEASED_APPID}").json()

    assert len(body["snapshots"]) == 2
    captured = [snapshot["captured_at"] for snapshot in body["snapshots"]]
    assert captured == sorted(captured, reverse=True)
    assert body["short_description"].startswith("A big-budget")
    assert body["on_windows"] is True


def test_refresh_returns_the_updated_history(client: TestClient):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    body = client.post(f"/games/{RELEASED_APPID}/refresh").json()

    assert len(body["snapshots"]) == 2
    assert body["latest_snapshot"]["captured_at"] == body["snapshots"][0]["captured_at"]


def test_history_limit_caps_returned_snapshots(client: TestClient):
    client.post("/games", json={"steam_appid": RELEASED_APPID})
    client.post(f"/games/{RELEASED_APPID}/refresh")

    body = client.get(f"/games/{RELEASED_APPID}", params={"history_limit": 1}).json()

    assert len(body["snapshots"]) == 1


def test_get_untracked_game_returns_404(client: TestClient):
    assert client.get(f"/games/{RELEASED_APPID}").status_code == 404


def test_refresh_untracked_game_returns_404(client: TestClient):
    assert client.post(f"/games/{RELEASED_APPID}/refresh").status_code == 404


def test_untrack_game(client: TestClient):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    assert client.delete(f"/games/{RELEASED_APPID}").status_code == 204
    assert client.get("/games").json() == []
    assert client.delete(f"/games/{RELEASED_APPID}").status_code == 404


def test_invalid_appid_is_rejected(client: TestClient):
    assert client.post("/games", json={"steam_appid": 0}).status_code == 422


def test_provisional_status_is_flagged_for_the_frontend(client: TestClient, engine):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    factory = sessionmaker(bind=engine)
    with factory() as db:
        game = db.query(Game).one()
        game.lifecycle_status = LifecycleStatus.FAILED_TO_MEET_EXPECTATIONS
        db.commit()

    status = client.get(f"/games/{RELEASED_APPID}").json()["status"]

    assert status["label"] == "Failed to Meet Expectations"
    assert status["provisional"] is True
    assert status["kind"] == "provisional"


def test_resolved_status_is_not_provisional(client: TestClient, engine):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    factory = sessionmaker(bind=engine)
    with factory() as db:
        game = db.query(Game).one()
        game.lifecycle_status = LifecycleStatus.RESOLVED
        game.resolved_outcome = Outcome.UNDERPERFORM
        db.commit()

    status = client.get(f"/games/{RELEASED_APPID}").json()["status"]

    assert status["label"] == "Underperform"
    assert status["provisional"] is False
    assert status["kind"] == "resolved"
