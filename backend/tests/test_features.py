"""Tests for the pre-launch feature contract.

The interesting assertions here are the negative ones. Every field in
`FORBIDDEN_FIELDS` would raise the model's measured accuracy while
destroying its actual purpose, and that failure is invisible in a score, so
the guard against it needs a test that fails loudly when someone removes it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.features import (
    FEATURE_NAMES,
    FORBIDDEN_FIELDS,
    LeakageError,
    PublisherHistory,
    assert_no_leakage,
    build_rows,
)
from app.models import (
    HistoricalRelease,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    WindowKey,
)


def test_current_feature_set_is_clean():
    assert_no_leakage()


@pytest.mark.parametrize(
    "field",
    ["positive_pct", "review_total", "retention_ratio", "studio_signal", "metacritic_score"],
)
def test_a_post_launch_field_is_rejected(field: str):
    with pytest.raises(LeakageError, match=field):
        assert_no_leakage((*FEATURE_NAMES, field))


def test_the_error_names_every_leaked_field_not_just_the_first():
    with pytest.raises(LeakageError) as caught:
        assert_no_leakage(("positive_pct", "launch_price_usd", "retention_ratio"))
    message = str(caught.value)
    assert "positive_pct" in message
    assert "retention_ratio" in message
    assert "launch_price_usd" not in message


def test_launch_window_review_fields_are_all_forbidden():
    # These are the ones that would flatter the model most: they are close to
    # a restatement of the label the rubric derives from the same numbers.
    for field in ("review_total", "review_positive", "positive_pct", "volume_percentile"):
        assert field in FORBIDDEN_FIELDS


def test_no_feature_name_is_repeated():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)


# --- publisher history: the two exclusions that make it pre-launch ---------


def add_release(
    session,
    appid,
    *,
    publisher,
    released,
    review_total,
    review_positive,
    outcome=None,
):
    release = HistoricalRelease(
        steam_appid=appid,
        game_name=f"Game {appid}",
        publisher=publisher,
        steam_release_date=released,
        cohort_year=released.year,
        launch_price_cents=6999,
        resolved_outcome=outcome,
        platform_launch_type=PlatformLaunchType.DAY_ONE_STEAM,
    )
    session.add(release)
    session.flush()
    session.add(
        ReleaseWindow(
            release_id=release.id,
            window_key=WindowKey.LAUNCH_2W,
            review_total=review_total,
            review_positive=review_positive,
        )
    )
    return release


def test_a_game_is_excluded_from_its_own_publishers_record(session):
    # Without this, a game's launch sentiment reaches its own feature vector
    # through the publisher average — and launch sentiment is most of what the
    # label is derived from.
    quiet = add_release(
        session,
        1,
        publisher="Acme",
        released=date(2022, 1, 1),
        review_total=100,
        review_positive=50,
    )
    loud = add_release(
        session,
        2,
        publisher="Acme",
        released=date(2023, 1, 1),
        review_total=100,
        review_positive=100,
    )
    session.commit()

    history = PublisherHistory.from_db(session)
    without_loud = history.record("Acme", before=None, exclude_release_id=loud.id)
    with_everything = history.record("Acme", before=None)

    assert without_loud.title_count == 1
    assert with_everything.title_count == 2
    assert without_loud.mean_positive_pct == 50.0
    assert with_everything.mean_positive_pct == 75.0
    assert quiet.id != loud.id


def test_a_publishers_later_games_do_not_inform_an_earlier_forecast(session):
    # Predicting a 2022 launch from a 2024 track record is not a forecast.
    add_release(
        session,
        1,
        publisher="Acme",
        released=date(2019, 1, 1),
        review_total=100,
        review_positive=60,
    )
    add_release(
        session,
        2,
        publisher="Acme",
        released=date(2024, 1, 1),
        review_total=100,
        review_positive=95,
    )
    session.commit()

    history = PublisherHistory.from_db(session)

    as_of_2022 = history.record("Acme", before=date(2022, 1, 1))
    assert as_of_2022.title_count == 1
    assert as_of_2022.mean_positive_pct == 60.0

    as_of_now = history.record("Acme", before=None)
    assert as_of_now.title_count == 2


def test_a_publishers_first_game_has_no_record_at_all(session):
    debut = add_release(
        session,
        1,
        publisher="Acme",
        released=date(2022, 1, 1),
        review_total=100,
        review_positive=90,
    )
    session.commit()

    record = PublisherHistory.from_db(session).record(
        "Acme", before=debut.steam_release_date, exclude_release_id=debut.id
    )

    assert record.title_count == 0
    assert not record.known


def test_build_rows_gives_a_debut_release_no_publisher_history(session):
    debut = add_release(
        session,
        1,
        publisher="Acme",
        released=date(2022, 1, 1),
        review_total=9000,
        review_positive=8500,
        outcome=Outcome.BREAKOUT,
    )
    session.commit()

    rows = build_rows(session)

    assert len(rows) == 1
    assert rows[0].steam_appid == debut.steam_appid
    assert rows[0].features[FEATURE_NAMES.index("publisher_title_count")] == 0.0
    assert rows[0].features[FEATURE_NAMES.index("publisher_known")] == 0.0


def test_build_rows_skips_delayed_ports(session):
    add_release(
        session,
        1,
        publisher="Acme",
        released=date(2022, 1, 1),
        review_total=100,
        review_positive=90,
        outcome=Outcome.SUCCESS,
    )
    port = add_release(
        session,
        2,
        publisher="Acme",
        released=date(2022, 6, 1),
        review_total=100,
        review_positive=90,
        outcome=Outcome.FLOP,
    )
    port.platform_launch_type = PlatformLaunchType.DELAYED_PORT
    session.commit()

    assert [row.steam_appid for row in build_rows(session)] == [1]
