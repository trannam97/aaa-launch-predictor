"""Tests for serving a trained forecast, and for the many ways there isn't one.

The absence cases carry the weight. The training job refuses to write an
artifact until the model beats a constant guess, so "no model" is the state
this endpoint spends most of its life in — and the wrong behaviour there is
not a crash but a silent one: serving a stale model, or a model fitted on a
feature set the current code no longer builds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.features import FEATURE_NAMES, build_live_features
from app.main import app, get_steam_client
from app.model_forecast import forecast as model_forecast
from app.model_forecast import reset_cache
from app.models import Game, HistoricalRelease, Outcome, PlatformLaunchType
from tests.conftest import RELEASED_APPID

numpy = pytest.importorskip("numpy")
pytest.importorskip("sklearn")
pytest.importorskip("joblib")


@pytest.fixture(autouse=True)
def clear_model_cache():
    reset_cache()
    yield
    reset_cache()


def make_game(session, **overrides) -> Game:
    fields = {
        "steam_appid": RELEASED_APPID,
        "name": "Upcoming Thing",
        "publishers": "Example Interactive",
        "developers": "Example Studios",
        "release_date": date(2026, 11, 6),
        "price_initial_cents": 6999,
        "on_windows": True,
    }
    fields.update(overrides)
    game = Game(**fields)
    session.add(game)
    session.flush()
    return game


def fitted_artifact(**overrides):
    """A real fitted model — small, but genuinely trained and pickleable."""
    from app.ordinal import MODEL_VERSION, Artifact, OrdinalGradientBoosting

    rng = numpy.random.default_rng(3)
    x = rng.normal(0, 1, (40, len(FEATURE_NAMES)))
    y = numpy.repeat([0, 1, 2, 3], 10)
    x[:, 0] = y + rng.normal(0, 0.3, 40)

    fields = {
        "model": OrdinalGradientBoosting(n_estimators=10).fit(x, y),
        "feature_names": FEATURE_NAMES,
        "version": MODEL_VERSION,
        "trained_at": datetime(2026, 8, 1, tzinfo=UTC),
        "n_rows": 40,
        "cv_accuracy": 0.55,
        "cv_distance": 0.6,
        "constant_accuracy": 0.35,
        "constant_distance": 1.0,
        "label_fingerprint": "40:abc",
    }
    fields.update(overrides)
    return Artifact(**fields)


@pytest.fixture
def artifact_path(tmp_path):
    from app.ordinal import save

    def write(**overrides):
        return save(fitted_artifact(**overrides), tmp_path / "outcome_model.joblib")

    return write


# --- absence -------------------------------------------------------------


def test_no_artifact_means_no_model_forecast(session, tmp_path):
    game = make_game(session)
    session.commit()

    assert model_forecast(session, game, tmp_path / "nothing.joblib") is None


def test_endpoint_falls_back_to_the_baseline_and_says_so(client):
    client.post("/games", json={"steam_appid": RELEASED_APPID})

    body = client.get(f"/games/{RELEASED_APPID}/prediction").json()

    assert body["method"] == "rule_based_baseline_v1"


def test_a_model_fitted_on_other_features_is_refused(session, artifact_path):
    # Adding or reordering a feature invalidates a stored model: the columns
    # would no longer line up, and nothing about the output would look wrong.
    path = artifact_path(feature_names=(*FEATURE_NAMES, "some_new_feature"))
    game = make_game(session)
    session.commit()

    assert model_forecast(session, game, path) is None


# --- serving -------------------------------------------------------------


def test_a_stored_model_produces_a_distribution(session, artifact_path):
    path = artifact_path()
    game = make_game(session)
    session.commit()

    result = model_forecast(session, game, path)

    assert result is not None
    assert result.method == "ordinal_gbt_v1"
    assert set(result.probabilities) == set(Outcome)
    assert pytest.approx(sum(result.probabilities.values()), abs=0.01) == 1.0
    assert result.predicted in Outcome


def test_the_forecast_names_what_it_had_to_assume(session, artifact_path):
    path = artifact_path()
    game = make_game(session)
    session.commit()

    result = model_forecast(session, game, path)

    assert any("Assumed, not observed" in line for line in result.basis)
    assert "less evidence" in result.rationale


def test_confidence_stays_low_while_the_corpus_is_small(session, artifact_path):
    path = artifact_path()
    game = make_game(session)
    session.commit()

    assert model_forecast(session, game, path).confidence == "low"


# --- live features -------------------------------------------------------


def test_live_features_match_the_training_vector_length(session):
    game = make_game(session)
    session.commit()

    live = build_live_features(session, game)

    assert len(live.values) == len(FEATURE_NAMES)


def test_unavailable_inputs_are_reported_not_hidden(session):
    game = make_game(session)
    session.commit()

    live = build_live_features(session, game)

    # None of these are ingested for an upcoming release.
    assert "budget_tier_aaa" in live.imputed
    assert "has_prelaunch_demo" in live.imputed
    assert "launch_day_dlc_count" in live.imputed
    # No publisher aggregates exist in an empty corpus either.
    assert "publisher_title_count" in live.imputed


def test_a_game_with_no_announced_date_imputes_its_cohort(session):
    game = make_game(session, release_date=None)
    session.commit()

    live = build_live_features(session, game)

    assert "cohort_year" in live.imputed
    assert "release_month" in live.imputed


def test_a_known_publisher_is_not_reported_as_imputed(session):
    from app.models import ReleaseWindow, WindowKey

    for appid in range(1, 4):
        release = HistoricalRelease(
            steam_appid=appid,
            game_name=f"Prior Game {appid}",
            publisher="Example Interactive",
            steam_release_date=date(2023, 3, 1),
            cohort_year=2023,
            platform_launch_type=PlatformLaunchType.DAY_ONE_STEAM,
        )
        session.add(release)
        session.flush()
        session.add(
            ReleaseWindow(
                release_id=release.id,
                window_key=WindowKey.LAUNCH_2W,
                review_total=4000,
                review_positive=3240,
            )
        )
    game = make_game(session)
    session.commit()

    live = build_live_features(session, game)

    assert "publisher_title_count" not in live.imputed
    assert live.values[FEATURE_NAMES.index("publisher_title_count")] == 3.0
    assert live.values[FEATURE_NAMES.index("publisher_mean_positive_pct")] == 81.0


def test_price_uses_a_recent_cohort_rate_when_the_release_year_has_none(session):
    # An upcoming 2026 release has no cohort of its own — the future has no
    # rows. Falling back to the last established rate beats assuming the game
    # is priced exactly at a going rate nobody has set yet.
    for appid in range(1, 12):
        session.add(
            HistoricalRelease(
                steam_appid=appid,
                game_name=f"Game {appid}",
                steam_release_date=date(2024, 5, 1),
                cohort_year=2024,
                launch_price_cents=6999,
                platform_launch_type=PlatformLaunchType.DAY_ONE_STEAM,
            )
        )
    game = make_game(session, price_initial_cents=4999)
    session.commit()

    live = build_live_features(session, game)

    assert "price_vs_going_rate" not in live.imputed
    assert live.values[FEATURE_NAMES.index("price_vs_going_rate")] == pytest.approx(0.714, abs=0.01)


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
