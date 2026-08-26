"""The pre-launch feature contract.

The model forecasts a game that has not shipped. Every feature here must be
knowable *before* release, and the separation is enforced rather than left to
discipline: `FORBIDDEN_FIELDS` names the post-launch columns, and
`assert_no_leakage()` fails loudly if one is ever added to the matrix.

That matters more here than it sounds. The corpus is full of fields that
would make the model look excellent and mean nothing — launch-window review
volume, launch sentiment, three-month retention, studio fate, post-launch
DLC. All of them are consequences of the outcome being predicted. A model
trained on them would score highly and forecast nothing, and the failure
would be invisible in the accuracy number.

It lives in the backend rather than under `/ml` because both sides of the
system need it and they must not drift: `/ml` fits a model on these vectors
and the API builds one per request to serve against. Two implementations of
"the feature vector" would disagree eventually, and the symptom would be a
quietly wrong forecast rather than an error. Nothing here imports
scikit-learn; only the code that fits models does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cohort import CohortIndex, PriceIndex
from app.companies import normalize_all
from app.models import (
    BudgetTier,
    DemoTiming,
    Game,
    HistoricalRelease,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    WindowKey,
)

# Columns that describe what happened at or after launch. Never features.
FORBIDDEN_FIELDS = frozenset(
    {
        "review_total",
        "review_positive",
        "review_negative",
        "positive_pct",
        "review_score_desc",
        "volume_percentile",
        "retention_ratio",
        "peak_concurrent_players",
        "studio_signal",
        "support_signal",
        "studio_outcome",
        "post_launch_support",
        "post_launch_dlc_count",
        "last_dlc_days_after_launch",
        "resolved_outcome",
        "label_confidence",
        # Critic scores land at launch under embargo, not before it.
        "metacritic_score",
    }
)

FEATURE_NAMES = (
    "publisher_mean_volume_pct",
    "publisher_mean_positive_pct",
    "publisher_title_count",
    "publisher_known",
    "price_vs_going_rate",
    "launch_price_usd",
    "budget_tier_aaa",
    "platform_count",
    "has_prelaunch_demo",
    "launch_day_dlc_count",
    "release_month",
    "cohort_year",
)


class LeakageError(AssertionError):
    """A post-launch field reached the feature matrix."""


def assert_no_leakage(names: tuple[str, ...] = FEATURE_NAMES) -> None:
    """Fail if any feature name collides with a known post-launch field."""
    leaked = sorted(n for n in names if n in FORBIDDEN_FIELDS)
    if leaked:
        raise LeakageError(
            f"post-launch fields used as pre-launch features: {leaked}. "
            "These are consequences of the outcome; a model using them would "
            "score well and forecast nothing."
        )


@dataclass(slots=True)
class TrainingRow:
    steam_appid: int
    game_name: str
    features: list[float]
    outcome: Outcome


# What a publisher's record defaults to when they have none in the corpus.
# Mid-scale rather than zero: an unknown publisher is not a bad publisher, and
# `publisher_known` is a separate feature so the model can tell the two apart.
UNKNOWN_PUBLISHER_VOLUME_PCT = 50.0
UNKNOWN_PUBLISHER_POSITIVE_PCT = 75.0


@dataclass(slots=True)
class PublisherRecord:
    mean_volume_pct: float
    mean_positive_pct: float
    title_count: int

    @property
    def known(self) -> bool:
        return self.title_count > 0


UNKNOWN_PUBLISHER = PublisherRecord(
    mean_volume_pct=UNKNOWN_PUBLISHER_VOLUME_PCT,
    mean_positive_pct=UNKNOWN_PUBLISHER_POSITIVE_PCT,
    title_count=0,
)


class PublisherHistory:
    """A publisher's track record *as of a given date*, excluding a given game.

    Both qualifications matter, and the stored `publisher_stats` table can
    provide neither — which is why this recomputes from the releases rather
    than reading it.

    **As of a date.** A publisher's later games had not happened when the game
    being forecast shipped. Averaging them in lets a 2019 launch be predicted
    from a 2024 track record, which is not a forecast.

    **Excluding a given game.** A publisher's mean launch volume includes the
    launch volume of the game being predicted, so without the exclusion the
    row's own outcome leaks into its own features through the aggregate. This
    is the leakage `ml/company_tiering.py` flagged and left for Phase 2.
    """

    def __init__(self, titles: dict[str, list[tuple[int, date, float | None, float | None]]]):
        self._titles = titles

    @classmethod
    def from_db(cls, session: Session) -> PublisherHistory:
        index = CohortIndex.from_db(session)
        windows = {
            w.release_id: w
            for w in session.scalars(
                select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_2W)
            )
        }

        titles: dict[str, list[tuple[int, date, float | None, float | None]]] = {}
        for release in session.scalars(select(HistoricalRelease)):
            if release.steam_release_date is None:
                continue
            window = windows.get(release.id)
            percentile = None
            if window is not None and window.review_total is not None:
                # The cohort this percentile is measured against includes the
                # release itself. One row among a couple of hundred moves a
                # percentile negligibly, and removing it would mean rebuilding
                # the index per game.
                percentile, _ = index.percentile(release.cohort_year, window.review_total)
            positive = window.positive_pct if window is not None else None
            entry = (release.id, release.steam_release_date, percentile, positive)
            for company in normalize_all(release.publisher):
                titles.setdefault(company, []).append(entry)
        return cls(titles)

    def record(
        self, publisher: str | None, *, before: date | None, exclude_release_id: int | None = None
    ) -> PublisherRecord:
        percentiles: list[float] = []
        sentiments: list[float] = []
        count = 0
        for name in normalize_all(publisher):
            for release_id, released, percentile, positive in self._titles.get(name, []):
                if release_id == exclude_release_id:
                    continue
                if before is not None and released >= before:
                    continue
                count += 1
                if percentile is not None:
                    percentiles.append(percentile)
                if positive is not None:
                    sentiments.append(positive)
            if count:
                # First matching normalization wins, as elsewhere: the
                # alternates are spellings of the same company, not extra ones.
                break

        if not count:
            return UNKNOWN_PUBLISHER
        return PublisherRecord(
            mean_volume_pct=(
                round(mean(percentiles), 2) if percentiles else UNKNOWN_PUBLISHER_VOLUME_PCT
            ),
            mean_positive_pct=(
                round(mean(sentiments), 2) if sentiments else UNKNOWN_PUBLISHER_POSITIVE_PCT
            ),
            title_count=count,
        )


def build_rows(session: Session, *, labeled_only: bool = True) -> list[TrainingRow]:
    """Assemble the pre-launch feature matrix.

    Day-one Steam releases only, for the same reason the rubric validation
    excludes ports: a delayed port's label describes a launch that happened
    on another platform, so its features and its label are about different
    events.
    """
    assert_no_leakage()

    prices = PriceIndex.from_db(session)
    publishers = PublisherHistory.from_db(session)

    query = select(HistoricalRelease)
    if labeled_only:
        query = query.where(HistoricalRelease.resolved_outcome.is_not(None))

    rows: list[TrainingRow] = []
    for release in session.scalars(query):
        if release.platform_launch_type is not PlatformLaunchType.DAY_ONE_STEAM:
            continue

        record = publishers.record(
            release.publisher,
            before=release.steam_release_date,
            exclude_release_id=release.id,
        )

        relative = prices.relative_price(release.cohort_year, release.launch_price_cents)
        features = [
            record.mean_volume_pct,
            record.mean_positive_pct,
            float(record.title_count),
            1.0 if record.known else 0.0,
            relative if relative is not None else 1.0,
            (release.launch_price_cents or 0) / 100.0,
            1.0 if release.budget_tier is BudgetTier.AAA else 0.0,
            float(sum((release.on_windows, release.on_mac, release.on_linux))),
            1.0 if release.demo_timing is DemoTiming.PRE_LAUNCH else 0.0,
            float(release.launch_day_dlc_count or 0),
            float(release.steam_release_date.month) if release.steam_release_date else 0.0,
            float(release.cohort_year or 0),
        ]
        if len(features) != len(FEATURE_NAMES):
            raise LeakageError("feature vector length does not match FEATURE_NAMES")

        rows.append(
            TrainingRow(
                steam_appid=release.steam_appid,
                game_name=release.game_name,
                features=features,
                outcome=release.resolved_outcome,
            )
        )

    return rows


@dataclass(slots=True)
class LiveFeatures:
    """A feature vector for a game that has not launched, plus its gaps.

    Three of the twelve features are recorded during historical backfill but
    are not part of what the tracker ingests for an upcoming release: budget
    tier, whether a demo shipped before launch, and how many DLC entries are
    listed on day one. They are filled with defaults here and named in
    `imputed`, so a forecast can say how much of its input was assumed rather
    than observed. Silently defaulting them would make a thinly-informed
    forecast indistinguishable from a well-informed one.
    """

    values: list[float]
    imputed: list[str]


# How far back to look for a cohort price when a game's own release year has
# too few historical rows to establish one. Upcoming releases always hit this:
# their cohort is the future, and the future has no rows yet.
PRICE_LOOKBACK_YEARS = 3


def _recent_going_rate(prices: PriceIndex, year: int) -> tuple[int | None, int | None]:
    for offset in range(PRICE_LOOKBACK_YEARS + 1):
        rate = prices.going_rate(year - offset)
        if rate:
            return rate, year - offset
    return None, None


def build_live_features(session: Session, game: Game) -> LiveFeatures:
    """The same twelve features as `build_rows`, from a tracked upcoming game.

    Kept next to `build_rows` on purpose. These two must produce the same
    vector in the same order or the model is scored on one thing and served
    another, and nothing would raise — the numbers would just be wrong.
    """
    assert_no_leakage()

    prices = PriceIndex.from_db(session)
    publishers = PublisherHistory.from_db(session)
    imputed: list[str] = []

    # No date cutoff: for a game that has not shipped, every historical
    # release in the corpus genuinely precedes it.
    record = publishers.record(game.publishers, before=None)
    if not record.known:
        imputed.extend(
            ["publisher_mean_volume_pct", "publisher_mean_positive_pct", "publisher_title_count"]
        )

    release_year = game.release_date.year if game.release_date else None
    release_month = game.release_date.month if game.release_date else 0
    if release_year is None:
        # A coming-soon game with no announced date. Its cohort is whichever
        # year it eventually lands in; the current one is the closest guess.
        release_year = datetime.now(UTC).year
        imputed.extend(["release_month", "cohort_year"])

    rate, _ = _recent_going_rate(prices, release_year)
    price_cents = game.price_initial_cents
    if rate and price_cents:
        relative = round(price_cents / rate, 3)
    else:
        relative = 1.0
        imputed.append("price_vs_going_rate")
    if not price_cents:
        imputed.append("launch_price_usd")

    # Not ingested for upcoming games — see LiveFeatures.
    imputed.extend(["budget_tier_aaa", "has_prelaunch_demo", "launch_day_dlc_count"])

    values = [
        record.mean_volume_pct,
        record.mean_positive_pct,
        float(record.title_count),
        1.0 if record.known else 0.0,
        relative,
        (price_cents or 0) / 100.0,
        # Everything the dashboard tracks is AAA by the project's own scope.
        1.0,
        float(sum((game.on_windows, game.on_mac, game.on_linux))),
        0.0,
        0.0,
        float(release_month),
        float(release_year),
    ]
    if len(values) != len(FEATURE_NAMES):
        raise LeakageError("live feature vector length does not match FEATURE_NAMES")

    return LiveFeatures(values=values, imputed=sorted(set(imputed)))
