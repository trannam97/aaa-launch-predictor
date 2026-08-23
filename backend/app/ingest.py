"""Phase 0 ingestion: pull one game's Steam data into the database.

This is the pipeline the later phases build on — Phase 0.5's historical
backfill reuses it for every field Steam can answer for, leaving only the
judgment fields (studio outcome, post-launch support, budget tier) to manual
or LLM research.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, GameSnapshot, LifecycleStatus, utcnow
from app.steam import AppDetails, SteamClient


@dataclass(slots=True)
class IngestResult:
    game: Game
    snapshot: GameSnapshot
    created: bool


def get_game_by_appid(session: Session, appid: int) -> Game | None:
    return session.scalar(select(Game).where(Game.steam_appid == appid))


def ingest_game(session: Session, appid: int, client: SteamClient) -> IngestResult:
    """Fetch a game from Steam and upsert it plus a fresh snapshot.

    The caller owns the transaction — this flushes but does not commit, so a
    job can batch several games into one commit.
    """
    details = client.get_app_details(appid)
    reviews = client.get_review_summary(appid)
    # An unreleased game has no meaningful player count; skip the request
    # rather than storing a zero that looks like real data.
    players = None if details.coming_soon else client.get_current_players(appid)

    game = get_game_by_appid(session, appid)
    created = game is None
    if game is None:
        game = Game(steam_appid=appid)
        session.add(game)

    _apply_details(game, details)
    game.lifecycle_status = _next_lifecycle_status(game.lifecycle_status, details)
    game.last_ingested_at = utcnow()

    snapshot = GameSnapshot(
        game=game,
        captured_at=utcnow(),
        price_final_cents=details.price_final_cents,
        discount_percent=details.discount_percent,
        review_total=reviews.total,
        review_positive=reviews.positive,
        review_negative=reviews.negative,
        review_score_desc=reviews.score_desc,
        concurrent_players=players,
        metacritic_score=details.metacritic_score,
    )
    session.add(snapshot)
    session.flush()
    return IngestResult(game=game, snapshot=snapshot, created=created)


def _apply_details(game: Game, details: AppDetails) -> None:
    game.name = details.name
    game.short_description = details.short_description
    game.header_image = details.header_image
    game.developers = _join(details.developers)
    game.publishers = _join(details.publishers)
    game.genres = _join(details.genres)
    game.release_date = details.release_date
    game.release_date_raw = details.release_date_raw
    game.coming_soon = details.coming_soon
    game.is_free = details.is_free
    game.price_initial_cents = details.price_initial_cents
    game.price_currency = details.price_currency
    game.on_windows = details.on_windows
    game.on_mac = details.on_mac
    game.on_linux = details.on_linux
    game.metacritic_score = details.metacritic_score
    game.metacritic_url = details.metacritic_url


def _next_lifecycle_status(current: LifecycleStatus | None, details: AppDetails) -> LifecycleStatus:
    """Advance the lifecycle on ingest, without ever walking it backwards.

    Ingest only knows one transition: pre_launch -> tracking, once Steam stops
    calling the game "coming soon". Everything past that (the provisional
    failed_to_meet_expectations state and its resolution) is the resolution
    job's call, not something a metadata refresh should undo.
    """
    if current is None:
        return LifecycleStatus.PRE_LAUNCH if details.coming_soon else LifecycleStatus.TRACKING
    if current is LifecycleStatus.PRE_LAUNCH and not details.coming_soon:
        return LifecycleStatus.TRACKING
    return current


def _join(values: list[str]) -> str | None:
    return "\n".join(values) if values else None


def split_list(value: str | None) -> list[str]:
    """Inverse of `_join`, for serializing back out to the API."""
    if not value:
        return []
    return [item for item in value.split("\n") if item]
