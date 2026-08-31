"""Tests for the rubric validation harness."""

from __future__ import annotations

from datetime import date

from app.models import (
    HistoricalRelease,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    StudioSignal,
    SupportSignal,
    WindowKey,
)
from app.validation import validate


def add(
    session,
    appid: int,
    outcome: Outcome | None,
    *,
    total_2w: int,
    positive_2w: int,
    total_3m: int | None = None,
    year: int = 2023,
    launch_type: PlatformLaunchType = PlatformLaunchType.DAY_ONE_STEAM,
    studio: StudioSignal = StudioSignal.CONTINUED,
    support: SupportSignal = SupportSignal.SUSTAINED,
):
    release = HistoricalRelease(
        steam_appid=appid,
        game_name=f"Game {appid}",
        steam_release_date=date(year, 6, 1),
        cohort_year=year,
        resolved_outcome=outcome,
        platform_launch_type=launch_type,
        studio_signal=studio,
        support_signal=support,
    )
    session.add(release)
    session.flush()
    session.add(
        ReleaseWindow(
            release_id=release.id,
            window_key=WindowKey.LAUNCH_2W,
            review_total=total_2w,
            review_positive=positive_2w,
            review_negative=total_2w - positive_2w,
        )
    )
    if total_3m is not None:
        session.add(
            ReleaseWindow(
                release_id=release.id,
                window_key=WindowKey.LAUNCH_3M,
                review_total=total_3m,
                review_positive=int(total_3m * positive_2w / total_2w),
                review_negative=total_3m - int(total_3m * positive_2w / total_2w),
            )
        )
    return release


def populate_cohort(session, start_appid: int = 100, n: int = 12):
    """Enough unlabeled peers that percentiles become reliable.

    Day-one and unlabeled, which is what the real cohort is made of. Ports
    were used here once to keep the peers out of scoring, but they are also
    kept out of the cohort index now — their Steam window measures a residual
    PC audience years after the console launch, not a launch — so a cohort
    built from them would be empty.
    """
    for i in range(n):
        add(
            session,
            start_appid + i,
            None,  # unlabeled: in the cohort, never scored against a hand label
            total_2w=1000 * (i + 1),
            positive_2w=int(1000 * (i + 1) * 0.9),
        )


def test_validation_scores_day_one_releases(session):
    populate_cohort(session)
    add(session, 1, Outcome.BREAKOUT, total_2w=50_000, positive_2w=47_000)
    session.commit()

    report = validate(session)

    assert len(report.scored) == 1
    assert report.scored[0].predicted is Outcome.BREAKOUT
    assert report.exact_agreement == 100.0


def test_validation_excludes_delayed_ports(session):
    populate_cohort(session)
    add(
        session,
        1,
        Outcome.SUCCESS,
        total_2w=5000,
        positive_2w=4500,
        launch_type=PlatformLaunchType.FORMER_EXCLUSIVE,
    )
    session.commit()

    report = validate(session)

    assert report.scored == []
    assert any("former_exclusive" in reason for _, reason in report.excluded)


def test_met_expectations_agreement_collapses_tiers(session):
    populate_cohort(session)
    # Labeled underperform, rubric will say flop — different tier, same side
    # of the met-expectations line.
    add(
        session,
        1,
        Outcome.UNDERPERFORM,
        total_2w=500,
        positive_2w=150,
        studio=StudioSignal.CLOSED,
        support=SupportSignal.ABANDONED,
    )
    session.commit()

    report = validate(session)

    assert report.exact_agreement == 0.0
    assert report.met_expectations_agreement == 100.0
    assert report.mean_tier_distance == 1.0


def test_ordinal_distance_penalises_bigger_misses(session):
    populate_cohort(session)
    add(session, 1, Outcome.FLOP, total_2w=50_000, positive_2w=48_000)
    session.commit()

    report = validate(session)

    # Predicted breakout against a flop label is the worst possible miss.
    assert report.mean_tier_distance == 3.0
    assert report.disagreements[0].tier_distance == 3


def test_release_without_window_data_is_excluded(session):
    populate_cohort(session)
    release = HistoricalRelease(
        steam_appid=1,
        game_name="No Windows",
        steam_release_date=date(2023, 6, 1),
        cohort_year=2023,
        resolved_outcome=Outcome.SUCCESS,
        platform_launch_type=PlatformLaunchType.DAY_ONE_STEAM,
    )
    session.add(release)
    session.commit()

    report = validate(session)

    assert any("no launch-window review data" in reason for _, reason in report.excluded)
