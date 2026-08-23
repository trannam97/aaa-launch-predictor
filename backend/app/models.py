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
