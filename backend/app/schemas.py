"""Pydantic response models for the API.

Status wording here is user-visible, so it follows the Responsible Framing
principle in PROJECT_SPEC.md: neutral, analyst-style labels, and a
provisional state that reads as unsettled rather than as a verdict.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.ingest import split_list
from app.models import Game, GameSnapshot, LifecycleStatus, Outcome

OUTCOME_LABELS: dict[Outcome, str] = {
    Outcome.FLOP: "Flop",
    Outcome.UNDERPERFORM: "Underperform",
    Outcome.SUCCESS: "Success",
    Outcome.BREAKOUT: "Breakout Success",
}

PROVISIONAL_LABEL = "Failed to Meet Expectations"


class TrackGameRequest(BaseModel):
    steam_appid: int = Field(gt=0, description="Steam application id, e.g. 1174180")


class StatusBadge(BaseModel):
    """What the dashboard should render for this game's current state.

    `provisional` exists so the frontend can style an unresolved outcome
    differently from a settled one — a game sitting at "Failed to Meet
    Expectations" has not been called a flop, and the UI must not imply it
    has.
    """

    label: str
    kind: str  # forecast | tracking | provisional | resolved | unresolved
    provisional: bool
    note: str | None = None


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    captured_at: datetime
    price_final_cents: int | None = None
    discount_percent: int | None = None
    review_total: int | None = None
    review_positive: int | None = None
    review_negative: int | None = None
    review_score_desc: str | None = None
    positive_pct: float | None = None
    concurrent_players: int | None = None
    metacritic_score: int | None = None

    @classmethod
    def from_model(cls, snapshot: GameSnapshot) -> SnapshotOut:
        return cls(
            captured_at=snapshot.captured_at,
            price_final_cents=snapshot.price_final_cents,
            discount_percent=snapshot.discount_percent,
            review_total=snapshot.review_total,
            review_positive=snapshot.review_positive,
            review_negative=snapshot.review_negative,
            review_score_desc=snapshot.review_score_desc,
            positive_pct=snapshot.positive_pct,
            concurrent_players=snapshot.concurrent_players,
            metacritic_score=snapshot.metacritic_score,
        )


class GameSummary(BaseModel):
    steam_appid: int
    name: str
    header_image: str | None = None
    release_date: date | None = None
    release_date_raw: str | None = None
    coming_soon: bool
    developers: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    price_initial_cents: int | None = None
    price_currency: str | None = None
    is_free: bool = False
    lifecycle_status: LifecycleStatus
    status: StatusBadge
    predicted_outcome: Outcome | None = None
    predicted_confidence: float | None = None
    resolved_outcome: Outcome | None = None
    # Critic and user scores stay two separate numbers, never blended.
    metacritic_score: int | None = None
    latest_snapshot: SnapshotOut | None = None
    last_ingested_at: datetime | None = None

    @classmethod
    def from_model(cls, game: Game, latest: GameSnapshot | None = None) -> GameSummary:
        """Build a summary. `latest` is passed in so callers control the query."""
        return cls(
            steam_appid=game.steam_appid,
            name=game.name,
            header_image=game.header_image,
            release_date=game.release_date,
            release_date_raw=game.release_date_raw,
            coming_soon=game.coming_soon,
            developers=split_list(game.developers),
            publishers=split_list(game.publishers),
            genres=split_list(game.genres),
            price_initial_cents=game.price_initial_cents,
            price_currency=game.price_currency,
            is_free=game.is_free,
            lifecycle_status=game.lifecycle_status,
            status=status_badge(game),
            predicted_outcome=game.predicted_outcome,
            predicted_confidence=game.predicted_confidence,
            resolved_outcome=game.resolved_outcome,
            metacritic_score=game.metacritic_score,
            latest_snapshot=SnapshotOut.from_model(latest) if latest else None,
            last_ingested_at=game.last_ingested_at,
        )


class GameDetail(GameSummary):
    short_description: str | None = None
    metacritic_url: str | None = None
    on_windows: bool = False
    on_mac: bool = False
    on_linux: bool = False
    snapshots: list[SnapshotOut] = Field(default_factory=list)

    @classmethod
    def from_model(cls, game: Game, snapshots: list[GameSnapshot] | None = None) -> GameDetail:
        # Snapshots arrive newest-first, from the caller's bounded query or
        # from the relationship's own ordering.
        history = snapshots if snapshots is not None else list(game.snapshots)
        base = GameSummary.from_model(game, history[0] if history else None).model_dump()
        return cls(
            **base,
            short_description=game.short_description,
            metacritic_url=game.metacritic_url,
            on_windows=game.on_windows,
            on_mac=game.on_mac,
            on_linux=game.on_linux,
            snapshots=[SnapshotOut.from_model(snapshot) for snapshot in history],
        )


def status_badge(game: Game) -> StatusBadge:
    """Map lifecycle + outcome onto the badge the dashboard shows."""
    status = game.lifecycle_status

    if status is LifecycleStatus.PRE_LAUNCH:
        if game.predicted_outcome is not None:
            return StatusBadge(
                label=f"Forecast: {OUTCOME_LABELS[game.predicted_outcome]}",
                kind="forecast",
                provisional=False,
                note="Pre-launch forecast of commercial performance, not a quality judgment.",
            )
        return StatusBadge(
            label="Awaiting forecast",
            kind="forecast",
            provisional=False,
            note="Tracked, but no prediction has been generated yet.",
        )

    if status is LifecycleStatus.TRACKING:
        return StatusBadge(
            label="Tracking",
            kind="tracking",
            provisional=False,
            note="Launched and being monitored; no outcome called yet.",
        )

    if status is LifecycleStatus.FAILED_TO_MEET_EXPECTATIONS:
        return StatusBadge(
            label=PROVISIONAL_LABEL,
            kind="provisional",
            provisional=True,
            note=(
                "Provisional: trending below expectations for its budget tier. "
                "Not yet resolved into a confirmed outcome."
            ),
        )

    if status is LifecycleStatus.UNRESOLVED_INSUFFICIENT_DATA:
        return StatusBadge(
            label=PROVISIONAL_LABEL,
            kind="unresolved",
            provisional=True,
            note="Insufficient public data to resolve this outcome further.",
        )

    if status is LifecycleStatus.RESOLVED and game.resolved_outcome is not None:
        return StatusBadge(
            label=OUTCOME_LABELS[game.resolved_outcome],
            kind="resolved",
            provisional=False,
            note="Resolved from post-launch commercial and support signals.",
        )

    # Marked resolved with no outcome recorded: report the gap, don't guess.
    return StatusBadge(
        label=PROVISIONAL_LABEL,
        kind="unresolved",
        provisional=True,
        note="Marked resolved but no outcome is recorded.",
    )
