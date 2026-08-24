"""SQLAlchemy models.

Phase 0 covers the two live-tracking tables: `games` (one row per tracked
Steam app) and `game_snapshots` (the time series we build ourselves, since
historical CCU/review history can't be backfilled — see the Data Layer
section of PROJECT_SPEC.md).

`historical_releases` (the training table) arrives with Phase 0.5 as its own
migration; it is deliberately not defined yet.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """Timezone-aware datetimes that survive a round-trip through SQLite.

    Postgres hands back aware datetimes; SQLite silently drops the offset and
    returns naive ones, so a value just written in-session would not compare
    against one read back. Everything is normalized to UTC on the way in and
    re-tagged as UTC on the way out, so both backends behave identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Naive input is assumed to already be UTC rather than rejected,
            # so a hand-written row or fixture doesn't blow up on save.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class LifecycleStatus(enum.StrEnum):
    """Where a game sits in the pipeline — distinct from its outcome.

    Kept separate from `predicted_outcome`/`resolved_outcome` so a null
    outcome can't be mistaken for "tracking fine" (PROJECT_SPEC.md, Schema
    Design for Lifecycle vs. Outcome).
    """

    PRE_LAUNCH = "pre_launch"
    TRACKING = "tracking"
    FAILED_TO_MEET_EXPECTATIONS = "failed_to_meet_expectations"
    RESOLVED = "resolved"
    UNRESOLVED_INSUFFICIENT_DATA = "unresolved_insufficient_data"


class Outcome(enum.StrEnum):
    """The four ordered commercial-performance tiers.

    Ordered worst-to-best; the integer ranks exist because the ordering is
    meaningful to the model (a Flop/Breakout confusion is a bigger miss than
    an Underperform/Success one) and to the UI.

    Note: "Failed to Meet Expectations" is NOT a member here. It is a
    lifecycle status meaning "not yet known", never a training label.
    """

    FLOP = "flop"
    UNDERPERFORM = "underperform"
    SUCCESS = "success"
    BREAKOUT = "breakout"

    @property
    def rank(self) -> int:
        return _OUTCOME_RANKS[self]


_OUTCOME_RANKS = {
    Outcome.FLOP: 0,
    Outcome.UNDERPERFORM: 1,
    Outcome.SUCCESS: 2,
    Outcome.BREAKOUT: 3,
}

# native_enum=False stores these as VARCHAR + CHECK constraint, which keeps
# the same migration working on both SQLite (local dev) and Postgres.
LifecycleStatusType = Enum(
    LifecycleStatus,
    name="lifecycle_status",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [member.value for member in cls],
)
OutcomeType = Enum(
    Outcome,
    name="outcome",
    native_enum=False,
    length=16,
    values_callable=lambda cls: [member.value for member in cls],
)


class Game(Base):
    """A tracked Steam release, plus its most recent structural metadata."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    steam_appid: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)

    # --- Steam metadata (refreshed on every ingest) ---
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text)
    header_image: Mapped[str | None] = mapped_column(String(512))
    developers: Mapped[str | None] = mapped_column(Text)  # newline-separated
    publishers: Mapped[str | None] = mapped_column(Text)  # newline-separated
    genres: Mapped[str | None] = mapped_column(Text)  # newline-separated

    release_date: Mapped[date | None] = mapped_column(Date)
    release_date_raw: Mapped[str | None] = mapped_column(String(64))
    coming_soon: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_free: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    price_initial_cents: Mapped[int | None] = mapped_column(Integer)
    price_currency: Mapped[str | None] = mapped_column(String(8))

    # Platform reach is an ML feature later; stored as flags for now.
    on_windows: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_mac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_linux: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    metacritic_score: Mapped[int | None] = mapped_column(Integer)
    metacritic_url: Mapped[str | None] = mapped_column(String(512))

    # --- Prediction lifecycle ---
    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(
        LifecycleStatusType, nullable=False, default=LifecycleStatus.PRE_LAUNCH
    )
    # Set once when the first forecast is made, then never overwritten, so it
    # stays comparable against resolved_outcome for accuracy tracking.
    predicted_outcome: Mapped[Outcome | None] = mapped_column(OutcomeType)
    predicted_confidence: Mapped[float | None] = mapped_column(Float)
    predicted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Ground truth, filled in by the resolution job. Null until resolved.
    resolved_outcome: Mapped[Outcome | None] = mapped_column(OutcomeType)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- Bookkeeping ---
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_ingested_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Lazy on purpose: snapshot history grows without bound as the polling
    # job runs, so it is loaded through an explicit, bounded query rather
    # than dragged along with every Game row.
    snapshots: Mapped[list[GameSnapshot]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameSnapshot.captured_at.desc()",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Game appid={self.steam_appid} name={self.name!r}>"


class GameSnapshot(Base):
    """One point-in-time reading of a game's volatile metrics.

    Rows accumulate at whatever cadence the polling job runs (see the
    Concurrent Player Polling notes in PROJECT_SPEC.md); anything older than
    ~30 days gets rolled up into daily aggregates in a later phase.
    """

    __tablename__ = "game_snapshots"
    __table_args__ = (
        UniqueConstraint("game_id", "captured_at", name="uq_game_snapshots_game_captured"),
        Index("ix_game_snapshots_game_captured", "game_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    price_final_cents: Mapped[int | None] = mapped_column(Integer)
    discount_percent: Mapped[int | None] = mapped_column(Integer)

    # Steam review data. Critic and user scores are kept as separate numbers
    # and never blended (PROJECT_SPEC.md, Public Reception Signal).
    review_total: Mapped[int | None] = mapped_column(Integer)
    review_positive: Mapped[int | None] = mapped_column(Integer)
    review_negative: Mapped[int | None] = mapped_column(Integer)
    review_score_desc: Mapped[str | None] = mapped_column(String(64))

    concurrent_players: Mapped[int | None] = mapped_column(Integer)
    metacritic_score: Mapped[int | None] = mapped_column(Integer)

    game: Mapped[Game] = relationship(back_populates="snapshots")

    @property
    def positive_pct(self) -> float | None:
        """Share of reviews that are positive, 0-100, or None if no reviews."""
        if not self.review_total:
            return None
        positive = self.review_positive or 0
        return round(100.0 * positive / self.review_total, 1)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<GameSnapshot game_id={self.game_id} at={self.captured_at}>"


class PlatformLaunchType(enum.StrEnum):
    """How a title arrived on Steam relative to its true first release.

    A console-first game reaching Steam a year later carries pre-existing
    reputation and pent-up demand into its Steam launch; a genuine day-one
    release has neither. Treating those as the same prediction problem
    throws away real signal.
    """

    DAY_ONE_STEAM = "day_one_steam"
    DELAYED_PORT = "delayed_port"
    FORMER_EXCLUSIVE = "former_exclusive"
    UNKNOWN = "unknown"


class BudgetTier(enum.StrEnum):
    """Coarse budget bracket. A $200M title and a $20M one need different bars."""

    AAA = "aaa"
    AA_LEANING_AAA = "aa_leaning_aaa"
    UNKNOWN = "unknown"


class LabelConfidence(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ResearchStatus(enum.StrEnum):
    """Where a row sits in the qualitative research pass.

    `unresolvable` is deliberately distinct from `not_researched`: it means
    someone looked and the public record genuinely doesn't settle the
    outcome, which is information — the same reasoning that keeps
    "Failed to Meet Expectations" separate from a confirmed Flop.
    """

    NOT_RESEARCHED = "not_researched"
    RESEARCHED = "researched"
    UNRESOLVABLE = "unresolvable"


class StudioSignal(enum.StrEnum):
    """What happened to the studio after launch, worst signal wins.

    Ordered by severity so a rule can compare them. This is the axis the spec
    says launch-window numbers cannot supply — peak players and review scores
    will not tell you whether a studio survived.
    """

    GREW = "grew"
    CONTINUED = "continued"
    SEVERE_LAYOFFS = "severe_layoffs"
    CLOSED = "closed"
    UNKNOWN = "unknown"

    @property
    def severity(self) -> int:
        return _STUDIO_SEVERITY[self]


_STUDIO_SEVERITY = {
    StudioSignal.GREW: 0,
    StudioSignal.CONTINUED: 1,
    StudioSignal.SEVERE_LAYOFFS: 2,
    StudioSignal.CLOSED: 3,
    StudioSignal.UNKNOWN: -1,
}


class SupportSignal(enum.StrEnum):
    """How post-launch support for the game itself played out.

    Separate from the studio's fate on purpose: a studio can be gutted and
    still finish the season pass (The Callisto Protocol), and a healthy studio
    can walk away from a title (Marvel's Avengers). Distinguishing Flop from
    Underperform needs both.
    """

    SUSTAINED = "sustained"
    CURTAILED = "curtailed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class DemoTiming(enum.StrEnum):
    """When a demo appeared relative to the game's Steam release.

    The distinction is the whole point. Of the corpus games that list a demo
    today, most got it *after* launch — publishers add one to convert
    holdouts when sales disappoint. So a bare "has a demo" flag correlates
    with commercial disappointment rather than predicting success, and only
    `PRE_LAUNCH` is usable as a pre-launch feature.

    `NONE_LISTED` means no demo is listed *now*, which is not the same as no
    demo ever existing: Steam Next Fest demos are routinely taken down after
    the event. Absence is not evidence of absence here — the same rule the
    spec already applies to wishlist figures.
    """

    PRE_LAUNCH = "pre_launch"
    LAUNCH_WINDOW = "launch_window"
    POST_LAUNCH = "post_launch"
    NONE_LISTED = "none_listed"
    UNKNOWN = "unknown"


class WindowKey(enum.StrEnum):
    """Named capture windows, measured from the Steam release date."""

    LAUNCH_2W = "launch_2w"
    LAUNCH_1M = "launch_1m"
    LAUNCH_3M = "launch_3m"
    LIFETIME = "lifetime"


PlatformLaunchTypeType = Enum(
    PlatformLaunchType,
    name="platform_launch_type",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
BudgetTierType = Enum(
    BudgetTier,
    name="budget_tier",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
LabelConfidenceType = Enum(
    LabelConfidence,
    name="label_confidence",
    native_enum=False,
    length=16,
    values_callable=lambda cls: [m.value for m in cls],
)
ResearchStatusType = Enum(
    ResearchStatus,
    name="research_status",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
StudioSignalType = Enum(
    StudioSignal,
    name="studio_signal",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
SupportSignalType = Enum(
    SupportSignal,
    name="support_signal",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
DemoTimingType = Enum(
    DemoTiming,
    name="demo_timing",
    native_enum=False,
    length=32,
    values_callable=lambda cls: [m.value for m in cls],
)
WindowKeyType = Enum(
    WindowKey,
    name="window_key",
    native_enum=False,
    length=16,
    values_callable=lambda cls: [m.value for m in cls],
)


class HistoricalRelease(Base):
    """One past AAA release in the training set.

    Fields split into three kinds, and the distinction matters when reading a
    row: **API-derived** (refetchable from Steam at any time), **curated**
    (human/LLM research that Steam cannot answer — studio outcome, budget
    tier, the label itself), and **provenance**.

    Only rows with a populated `resolved_outcome` are eligible for training.
    """

    __tablename__ = "historical_releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    steam_appid: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)

    # --- API-derived ---
    game_name: Mapped[str] = mapped_column(String(255), nullable=False)
    developer: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[str | None] = mapped_column(Text)
    steam_release_date: Mapped[date | None] = mapped_column(Date)
    # Steam's `price_overview.initial` is today's list price, not the launch
    # price — publishers permanently re-tier older titles (The Witcher 3
    # launched at $59.99 and now lists at $39.99). The launch figure is
    # therefore curated, and this API value kept only for reference.
    current_list_price_cents: Mapped[int | None] = mapped_column(Integer)
    price_currency: Mapped[str | None] = mapped_column(String(8))
    on_windows: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_mac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_linux: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metacritic_score: Mapped[int | None] = mapped_column(Integer)
    metacritic_url: Mapped[str | None] = mapped_column(String(512))
    # Demo, if Steam currently lists one. Timing is what carries the signal;
    # see DemoTiming for why a bare presence flag would mislead.
    demo_appid: Mapped[int | None] = mapped_column(Integer)
    demo_release_date: Mapped[date | None] = mapped_column(Date)
    demo_timing: Mapped[DemoTiming] = mapped_column(
        DemoTimingType, nullable=False, default=DemoTiming.UNKNOWN
    )
    # Add-on content. `dlc_count` counts only content sold as separate Steam
    # apps — Helldivers 2 shows zero because Warbonds are bought with in-game
    # currency, so this is not a measure of content volume. Read it with
    # `has_in_app_purchases`, which catches the models it misses.
    #
    # The split by timing is the useful part. Launch-day DLC is a pre-launch
    # monetization decision and is safe as a forecasting feature; DLC shipped
    # later is a post-launch support signal and is outcome-contaminated.
    dlc_count: Mapped[int | None] = mapped_column(Integer)
    launch_day_dlc_count: Mapped[int | None] = mapped_column(Integer)
    post_launch_dlc_count: Mapped[int | None] = mapped_column(Integer)
    last_dlc_days_after_launch: Mapped[int | None] = mapped_column(Integer)
    has_in_app_purchases: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Grouping key for cohort normalization: raw counts are not comparable
    # across years, so every count-based feature is ranked within its cohort.
    cohort_year: Mapped[int | None] = mapped_column(Integer, index=True)

    # --- Curated research ---
    original_release_date: Mapped[date | None] = mapped_column(Date)
    launch_price_cents: Mapped[int | None] = mapped_column(Integer)
    platform_launch_type: Mapped[PlatformLaunchType] = mapped_column(
        PlatformLaunchTypeType, nullable=False, default=PlatformLaunchType.UNKNOWN
    )
    platform_reach: Mapped[str | None] = mapped_column(String(128))
    budget_tier: Mapped[BudgetTier] = mapped_column(
        BudgetTierType, nullable=False, default=BudgetTier.UNKNOWN
    )
    post_launch_support: Mapped[str | None] = mapped_column(Text)
    studio_outcome: Mapped[str | None] = mapped_column(Text)
    # Structured counterparts to the two prose fields above — the rubric runs
    # on these; the prose stays for a human reading the row.
    studio_signal: Mapped[StudioSignal] = mapped_column(
        StudioSignalType, nullable=False, default=StudioSignal.UNKNOWN
    )
    support_signal: Mapped[SupportSignal] = mapped_column(
        SupportSignalType, nullable=False, default=SupportSignal.UNKNOWN
    )
    resolved_outcome: Mapped[Outcome | None] = mapped_column(OutcomeType)
    label_confidence: Mapped[LabelConfidence | None] = mapped_column(LabelConfidenceType)
    research_status: Mapped[ResearchStatus] = mapped_column(
        ResearchStatusType, nullable=False, default=ResearchStatus.NOT_RESEARCHED
    )

    # --- Provenance ---
    notes: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[str | None] = mapped_column(Text)
    backfilled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    windows: Mapped[list[ReleaseWindow]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleaseWindow.window_key",
    )

    @property
    def is_trainable(self) -> bool:
        """Only resolved rows may enter training — an unlabeled row is excluded."""
        return self.resolved_outcome is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<HistoricalRelease appid={self.steam_appid} name={self.game_name!r}>"


class ReleaseWindow(Base):
    """Metrics for one named window of a historical release.

    Windowed rather than lifetime, because lifetime totals fold years of
    later sales and sentiment into what is supposed to be a launch-window
    measurement.

    `peak_concurrent_players` is nullable and, for backfilled games, will
    stay null: Steam publishes only a current player count, so historical CCU
    genuinely cannot be recovered. Review figures, by contrast, can be —
    Steam's review endpoint accepts a date range.
    """

    __tablename__ = "release_windows"
    __table_args__ = (
        UniqueConstraint("release_id", "window_key", name="uq_release_windows_release_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("historical_releases.id", ondelete="CASCADE"), nullable=False
    )
    window_key: Mapped[WindowKey] = mapped_column(WindowKeyType, nullable=False)
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)

    review_total: Mapped[int | None] = mapped_column(Integer)
    review_positive: Mapped[int | None] = mapped_column(Integer)
    review_negative: Mapped[int | None] = mapped_column(Integer)
    review_score_desc: Mapped[str | None] = mapped_column(String(64))
    peak_concurrent_players: Mapped[int | None] = mapped_column(Integer)

    release: Mapped[HistoricalRelease] = relationship(back_populates="windows")

    @property
    def positive_pct(self) -> float | None:
        if not self.review_total:
            return None
        return round(100.0 * (self.review_positive or 0) / self.review_total, 1)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ReleaseWindow release_id={self.release_id} key={self.window_key}>"
