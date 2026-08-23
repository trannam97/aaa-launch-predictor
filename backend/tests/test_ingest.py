"""Tests for the Phase 0 ingestion pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.ingest import ingest_game
from app.models import Game, GameSnapshot, LifecycleStatus, Outcome
from tests.conftest import RELEASED_APPID, UPCOMING_APPID, load_fixture


def test_ingest_creates_game_and_snapshot(session, steam_client):
    result = ingest_game(session, RELEASED_APPID, steam_client)
    session.commit()

    assert result.created is True
    game = session.scalar(select(Game).where(Game.steam_appid == RELEASED_APPID))
    assert game is not None
    assert game.name == "Example Released Game"
    assert game.release_date == date(2018, 10, 26)
    assert game.publishers == "Example Interactive"
    assert game.genres == "Action\nAdventure"
    assert game.last_ingested_at is not None

    snapshot = session.scalar(select(GameSnapshot).where(GameSnapshot.game_id == game.id))
    assert snapshot.review_total == 96257
    assert snapshot.review_positive == 84213
    assert snapshot.concurrent_players == 18342
    assert snapshot.price_final_cents == 2999
    assert snapshot.discount_percent == 50
    assert snapshot.positive_pct == 87.5


def test_reingest_updates_game_and_appends_snapshot(session, steam_client_factory):
    first = steam_client_factory()
    ingest_game(session, RELEASED_APPID, first)
    session.commit()

    discounted = load_fixture("appdetails_released.json")
    discounted["1174180"]["data"]["price_overview"]["final"] = 1499
    discounted["1174180"]["data"]["price_overview"]["discount_percent"] = 75
    second = steam_client_factory(
        details=discounted, players={"response": {"player_count": 9001, "result": 1}}
    )

    result = ingest_game(session, RELEASED_APPID, second)
    session.commit()

    assert result.created is False

    snapshots = session.scalars(select(GameSnapshot).order_by(GameSnapshot.captured_at)).all()
    assert len(snapshots) == 2
    assert [s.price_final_cents for s in snapshots] == [2999, 1499]
    assert snapshots[-1].concurrent_players == 9001
    # One game row, not two.
    assert len(session.scalars(select(Game)).all()) == 1


def test_ingest_of_upcoming_game_skips_player_count(session, steam_client_factory):
    client = steam_client_factory(
        details=load_fixture("appdetails_upcoming.json"),
        reviews=load_fixture("appreviews_empty.json"),
        players={"response": {"player_count": 12345, "result": 1}},
    )

    result = ingest_game(session, UPCOMING_APPID, client)
    session.commit()

    assert result.game.lifecycle_status is LifecycleStatus.PRE_LAUNCH
    assert result.game.coming_soon is True
    # A pre-launch game has no players; storing a number here would be noise.
    assert result.snapshot.concurrent_players is None
    assert result.snapshot.positive_pct is None


def test_lifecycle_advances_from_pre_launch_to_tracking(session, steam_client_factory):
    upcoming = steam_client_factory(
        details=load_fixture("appdetails_upcoming.json"),
        reviews=load_fixture("appreviews_empty.json"),
    )
    game = ingest_game(session, UPCOMING_APPID, upcoming).game
    session.commit()
    assert game.lifecycle_status is LifecycleStatus.PRE_LAUNCH

    launched = load_fixture("appdetails_upcoming.json")
    launched["2000950"]["data"]["release_date"] = {"coming_soon": False, "date": "Nov 12, 2026"}
    released_client = steam_client_factory(
        details=launched, reviews=load_fixture("appreviews_released.json")
    )

    game = ingest_game(session, UPCOMING_APPID, released_client).game
    session.commit()

    assert game.lifecycle_status is LifecycleStatus.TRACKING
    assert game.release_date == date(2026, 11, 12)


def test_ingest_never_walks_lifecycle_backwards(session, steam_client):
    game = ingest_game(session, RELEASED_APPID, steam_client).game
    game.lifecycle_status = LifecycleStatus.FAILED_TO_MEET_EXPECTATIONS
    session.commit()

    game = ingest_game(session, RELEASED_APPID, steam_client).game
    session.commit()

    # A routine metadata refresh must not undo the resolution job's work.
    assert game.lifecycle_status is LifecycleStatus.FAILED_TO_MEET_EXPECTATIONS


def test_ingest_leaves_prediction_fields_untouched(session, steam_client):
    game = ingest_game(session, RELEASED_APPID, steam_client).game
    predicted_at = datetime.now(UTC) - timedelta(days=30)
    game.predicted_outcome = Outcome.SUCCESS
    game.predicted_confidence = 0.62
    game.predicted_at = predicted_at
    session.commit()

    ingest_game(session, RELEASED_APPID, steam_client)
    session.commit()

    # The pre-launch forecast is immutable — accuracy tracking depends on it.
    assert game.predicted_outcome is Outcome.SUCCESS
    assert game.predicted_confidence == 0.62
    assert game.resolved_outcome is None


def test_deleting_a_game_deletes_its_snapshots(session, steam_client):
    game = ingest_game(session, RELEASED_APPID, steam_client).game
    session.commit()

    session.delete(game)
    session.commit()

    assert session.scalars(select(GameSnapshot)).all() == []
