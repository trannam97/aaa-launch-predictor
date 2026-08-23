"""FastAPI application — Phase 0 endpoints.

Scope is deliberately narrow: track a Steam app, refresh its data, read it
back. No ML model and no LLM layer yet (Phases 2 and 3).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.ingest import get_game_by_appid, ingest_game
from app.models import Game, GameSnapshot
from app.schemas import GameDetail, GameSummary, TrackGameRequest
from app.steam import SteamAppNotFound, SteamClient, SteamError, SteamUnavailable

# How many snapshots a game detail response carries by default. Snapshot
# history grows without bound as the polling job runs, so every endpoint that
# returns a detail payload caps it.
DEFAULT_HISTORY_LIMIT = 60

DISCLAIMER = (
    "Not affiliated with Valve. Predictions estimate commercial performance "
    "relative to budget-tier expectations, not creative quality."
)

app = FastAPI(
    title="AAA Game Launch Predictor API",
    version="0.1.0",
    description=("Phase 0: Steam data ingestion for tracked releases. " + DISCLAIMER),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_steam_client() -> Iterator[SteamClient]:
    """One Steam client per request; overridden in tests."""
    client = SteamClient()
    try:
        yield client
    finally:
        client.close()


def _ingest_or_http_error(session: Session, appid: int, client: SteamClient):
    """Run an ingest, translating Steam failures into HTTP responses."""
    try:
        return ingest_game(session, appid, client)
    except SteamAppNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SteamUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except SteamError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _recent_snapshots(
    session: Session, game: Game, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[GameSnapshot]:
    """The newest `limit` snapshots for a game, newest first."""
    return list(
        session.scalars(
            select(GameSnapshot)
            .where(GameSnapshot.game_id == game.id)
            .order_by(GameSnapshot.captured_at.desc())
            .limit(limit)
        )
    )


def _latest_snapshot_by_game(session: Session, games: list[Game]) -> dict[int, GameSnapshot]:
    """The newest snapshot for each of `games`, in one query rather than N."""
    game_ids = [game.id for game in games]
    if not game_ids:
        return {}

    newest = (
        select(
            GameSnapshot.game_id,
            func.max(GameSnapshot.captured_at).label("captured_at"),
        )
        .where(GameSnapshot.game_id.in_(game_ids))
        .group_by(GameSnapshot.game_id)
        .subquery()
    )
    snapshots = session.scalars(
        select(GameSnapshot).join(
            newest,
            (GameSnapshot.game_id == newest.c.game_id)
            & (GameSnapshot.captured_at == newest.c.captured_at),
        )
    )
    return {snapshot.game_id: snapshot for snapshot in snapshots}


def _require_game(session: Session, appid: int) -> Game:
    game = get_game_by_appid(session, appid)
    if game is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"appid {appid} is not tracked"
        )
    return game


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "phase": "0", "disclaimer": DISCLAIMER}


@app.get("/games", response_model=list[GameSummary], tags=["games"])
def list_games(session: Session = Depends(get_db)) -> list[GameSummary]:
    """All tracked games, soonest release first, undated ones last."""
    games = list(
        session.scalars(
            select(Game).order_by(Game.release_date.is_(None), Game.release_date, Game.name)
        )
    )
    latest = _latest_snapshot_by_game(session, games)
    return [GameSummary.from_model(game, latest.get(game.id)) for game in games]


@app.post(
    "/games",
    response_model=GameDetail,
    status_code=status.HTTP_201_CREATED,
    tags=["games"],
)
def track_game(
    payload: TrackGameRequest,
    response: Response,
    session: Session = Depends(get_db),
    client: SteamClient = Depends(get_steam_client),
) -> GameDetail:
    """Start tracking a Steam app and ingest its data immediately.

    Tracking an already-tracked app refreshes it and returns 200 instead of
    201, so a retried request is harmless.
    """
    result = _ingest_or_http_error(session, payload.steam_appid, client)
    session.commit()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return GameDetail.from_model(result.game, snapshots=_recent_snapshots(session, result.game))


@app.get("/games/{appid}", response_model=GameDetail, tags=["games"])
def get_game(
    appid: int,
    history_limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> GameDetail:
    """One tracked game plus its most recent snapshots (newest first)."""
    game = _require_game(session, appid)
    return GameDetail.from_model(game, snapshots=_recent_snapshots(session, game, history_limit))


@app.post("/games/{appid}/refresh", response_model=GameDetail, tags=["games"])
def refresh_game(
    appid: int,
    session: Session = Depends(get_db),
    client: SteamClient = Depends(get_steam_client),
) -> GameDetail:
    """Re-fetch a tracked game from Steam and append a new snapshot."""
    _require_game(session, appid)
    result = _ingest_or_http_error(session, appid, client)
    session.commit()
    return GameDetail.from_model(result.game, snapshots=_recent_snapshots(session, result.game))


@app.delete("/games/{appid}", status_code=status.HTTP_204_NO_CONTENT, tags=["games"])
def untrack_game(appid: int, session: Session = Depends(get_db)) -> Response:
    """Stop tracking a game and delete its snapshot history."""
    game = _require_game(session, appid)
    session.delete(game)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
