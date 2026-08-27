"""Tests for what belongs in a cohort — and what a cohort is comparing.

The cohort answers "what did a normal launch look like that year". Everything
here is about keeping events that are not launches out of that reference set,
because contamination there does not raise an error: it silently shifts every
percentile computed against it.
"""

from __future__ import annotations

from datetime import date

from app.cohort import CohortIndex, PriceIndex
from app.models import HistoricalRelease, PlatformLaunchType, ReleaseWindow, WindowKey


def add_release(session, appid, *, launch_type, review_total, year=2023, price=6000):
    release = HistoricalRelease(
        steam_appid=appid,
        game_name=f"Game {appid}",
        steam_release_date=date(year, 6, 1),
        cohort_year=year,
        launch_price_cents=price,
        platform_launch_type=launch_type,
    )
    session.add(release)
    session.flush()
    session.add(
        ReleaseWindow(
            release_id=release.id,
            window_key=WindowKey.LAUNCH_2W,
            review_total=review_total,
            review_positive=int(review_total * 0.8),
        )
    )
    return release


def test_delayed_ports_are_kept_out_of_the_volume_cohort(session):
    # A port's Steam window measures whatever PC audience remained after the
    # console launch, often years earlier — systematically smaller, and not a
    # launch. Leaving them in drags the distribution down and inflates every
    # day-one game's percentile.
    for i in range(10):
        add_release(
            session,
            100 + i,
            launch_type=PlatformLaunchType.DAY_ONE_STEAM,
            review_total=10_000 + i * 1000,
        )
    for i in range(10):
        add_release(
            session,
            200 + i,
            launch_type=PlatformLaunchType.DELAYED_PORT,
            review_total=100 + i,
        )
    session.commit()

    stats = CohortIndex.from_db(session).stats_for(2023)

    assert len(stats.values) == 10
    assert min(stats.values) >= 10_000


def test_former_exclusives_are_excluded_too(session):
    for i in range(10):
        add_release(
            session, 100 + i, launch_type=PlatformLaunchType.DAY_ONE_STEAM, review_total=5_000
        )
    add_release(session, 300, launch_type=PlatformLaunchType.FORMER_EXCLUSIVE, review_total=7)
    session.commit()

    assert 7 not in CohortIndex.from_db(session).stats_for(2023).values


def test_an_unknown_launch_type_is_excluded_rather_than_assumed(session):
    # A row whose launch type has not been established could be either, so it
    # is left out rather than counted as a launch.
    for i in range(10):
        add_release(
            session, 100 + i, launch_type=PlatformLaunchType.DAY_ONE_STEAM, review_total=5_000
        )
    add_release(session, 400, launch_type=PlatformLaunchType.UNKNOWN, review_total=9)
    session.commit()

    assert 9 not in CohortIndex.from_db(session).stats_for(2023).values


def test_unlabeled_day_one_releases_still_count(session):
    # The cohort describes the year, not the research backlog.
    for i in range(10):
        add_release(
            session, 100 + i, launch_type=PlatformLaunchType.DAY_ONE_STEAM, review_total=5_000
        )
    session.commit()

    assert len(CohortIndex.from_db(session).stats_for(2023).values) == 10


def test_the_price_index_applies_the_same_rule(session):
    for i in range(10):
        add_release(
            session,
            100 + i,
            launch_type=PlatformLaunchType.DAY_ONE_STEAM,
            review_total=5_000,
            price=7000,
        )
    for i in range(9):
        add_release(
            session,
            200 + i,
            launch_type=PlatformLaunchType.DELAYED_PORT,
            review_total=100,
            price=2000,
        )
    session.commit()

    # A years-old game arriving on PC at $20 says nothing about the going rate
    # for a new release that year.
    assert PriceIndex.from_db(session).going_rate(2023) == 7000
