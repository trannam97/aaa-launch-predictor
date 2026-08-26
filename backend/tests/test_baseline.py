"""Tests for the rule-based pre-launch baseline and its API endpoint."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.baseline import MIN_TRACK_RECORD, TIERS, base_rate, forecast
from app.db import get_db
from app.main import app, get_steam_client
from app.models import HistoricalRelease, Outcome, PlatformLaunchType
from tests.conftest import RELEASED_APPID


def add_release(
    session,
    appid: int,
    outcome: Outcome | None,
    *,
    developer: str = "Example Studios",
    publisher: str = "Example Interactive",
    launch_type: PlatformLaunchType = PlatformLaunchType.DAY_ONE_STEAM,
) -> HistoricalRelease:
    release = HistoricalRelease(
        steam_appid=appid,
        game_name=f"Game {appid}",
        developer=developer,
        publisher=publisher,
        steam_release_date=date(2023, 1, 1),
        cohort_year=2023,
        resolved_outcome=outcome,
        platform_launch_type=launch_type,
    )
    session.add(release)
    session.flush()
    return release


def test_base_rate_sums_to_one_and_smooths_empty_tiers(session):
    add_release(session, 1, Outcome.FLOP)
    add_release(session, 2, Outcome.FLOP)
    session.commit()

    rates = base_rate(session)

    assert pytest.approx(sum(rates.values()), abs=0.001) == 1.0
    # No tier gets probability zero on the strength of two examples.
    assert all(rate > 0 for rate in rates.values())
    assert rates[Outcome.FLOP] == max(rates.values())


def test_base_rate_ignores_delayed_ports(session):
    add_release(session, 1, Outcome.BREAKOUT)
    add_release(session, 2, Outcome.FLOP, launch_type=PlatformLaunchType.DELAYED_PORT)
    add_release(session, 3, Outcome.FLOP, launch_type=PlatformLaunchType.FORMER_EXCLUSIVE)
    session.commit()

    rates = base_rate(session)

    assert rates[Outcome.BREAKOUT] == max(rates.values())


def test_base_rate_ignores_unlabeled_rows(session):
    add_release(session, 1, Outcome.SUCCESS)
    add_release(session, 2, None)
    session.commit()

    rates = base_rate(session)

    assert rates[Outcome.SUCCESS] == max(rates.values())


def test_track_record_shifts_the_forecast(session):
    # A team whose previous games all flopped should not get the dataset's
    # average outlook.
    for appid in range(1, 5):
        add_release(session, appid, Outcome.FLOP, developer="Unlucky Studios")
    for appid in range(10, 14):
        add_release(session, appid, Outcome.BREAKOUT, developer="Other Studios")
    session.commit()

    unlucky = forecast(session, developer="Unlucky Studios", publisher="")
    other = forecast(session, developer="Other Studios", publisher="")

    assert unlucky.probabilities[Outcome.FLOP] > other.probabilities[Outcome.FLOP]
    assert any("prior labeled release" in line for line in unlucky.basis)


def test_thin_track_record_is_not_trusted(session):
    add_release(session, 1, Outcome.BREAKOUT, developer="One Hit Studios")
    for appid in range(10, 16):
        add_release(session, appid, Outcome.FLOP, developer="Other Studios")
    session.commit()

    result = forecast(session, developer="One Hit Studios", publisher="")

    assert MIN_TRACK_RECORD > 1
    assert any("too few" in line for line in result.basis)
    assert result.predicted is Outcome.FLOP  # base rate still dominates


def test_forecast_excludes_the_game_itself(session):
    # A tracked game already in historical_releases must not be used as
    # evidence for its own forecast.
    add_release(session, 500, Outcome.BREAKOUT, developer="Solo Studios")
    add_release(session, 501, Outcome.FLOP, developer="Solo Studios")
    session.commit()

    with_self = forecast(session, developer="Solo Studios", publisher="")
    without = forecast(session, developer="Solo Studios", publisher="", exclude_appid=500)

    assert with_self.probabilities[Outcome.BREAKOUT] > without.probabilities[Outcome.BREAKOUT]


def test_probabilities_are_a_distribution(session):
    add_release(session, 1, Outcome.SUCCESS)
    session.commit()

    result = forecast(session, developer="Nobody", publisher="Nobody")

    assert set(result.probabilities) == set(TIERS)
    assert pytest.approx(sum(result.probabilities.values()), abs=0.001) == 1.0
    assert result.predicted_probability == max(result.probabilities.values())


def test_baseline_never_claims_high_confidence(session):
    for appid in range(1, 8):
        add_release(session, appid, Outcome.BREAKOUT, developer="Great Studios")
    session.commit()

    result = forecast(session, developer="Great Studios", publisher="")

    # A structural prior is a floor, not a confident call.
    assert result.confidence == "low"
    assert "structural prior" in result.rationale


# --- endpoint -------------------------------------------------------------


@pytest.fixture
def client(engine, steam_client):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_steam_client] = lambda: steam_client
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_prediction_endpoint(client, engine):
    client.post("/games", json={"steam_appid": RELEASED_APPID})
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        add_release(db, 1, Outcome.SUCCESS)
        db.commit()

    body = client.get(f"/games/{RELEASED_APPID}/prediction").json()

    assert body["steam_appid"] == RELEASED_APPID
    assert body["method"] == "rule_based_baseline_v1"
    assert body["confidence"] == "low"
    assert set(body["probabilities"]) == {"flop", "underperform", "success", "breakout"}
    assert body["predicted_label"]
    assert body["basis"]


def test_prediction_for_untracked_game_is_404(client):
    assert client.get("/games/424242/prediction").status_code == 404
