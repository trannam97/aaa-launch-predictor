#!/usr/bin/env python3
"""Refresh Steam data for tracked games.

Phase 0's scheduled job. Run it with no arguments to refresh everything
already in the database, or with --appid to add/refresh specific games:

    python jobs/refresh_tracked_games.py
    python jobs/refresh_tracked_games.py --appid 1174180 --appid 2000950

Requires DATABASE_URL (see .env.example). The Steam endpoints used here are
public and need no key.

Exits non-zero if any game failed, so a scheduled run surfaces as a failure
rather than passing quietly.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.ingest import ingest_game  # noqa: E402
from app.models import Game  # noqa: E402
from app.steam import SteamClient, SteamError  # noqa: E402

# Steam tolerates roughly 200 store requests per 5 minutes per IP, and each
# game costs up to three. A small gap between games keeps a full refresh well
# under that without needing real rate-limit bookkeeping.
DELAY_BETWEEN_GAMES_SECONDS = 1.5

logger = logging.getLogger("refresh_tracked_games")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--appid",
        type=int,
        action="append",
        dest="appids",
        help="Refresh only this appid (repeatable). Adds it if not yet tracked.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_GAMES_SECONDS,
        help="Seconds to wait between games (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    with session_scope() as session:
        appids = args.appids or list(
            session.scalars(select(Game.steam_appid).order_by(Game.steam_appid))
        )

        if not appids:
            logger.info("No tracked games; nothing to refresh.")
            return 0

        logger.info("Refreshing %d game(s)", len(appids))
        failures = 0
        with SteamClient() as client:
            for index, appid in enumerate(appids):
                if index:
                    time.sleep(args.delay)
                try:
                    result = ingest_game(session, appid, client)
                except SteamError as exc:
                    failures += 1
                    logger.error("appid %s failed: %s", appid, exc)
                    continue
                # Commit per game so a run that dies partway through keeps
                # the games it already fetched.
                session.commit()
                logger.info(
                    "appid %s %s: %s (%s)",
                    appid,
                    "added" if result.created else "refreshed",
                    result.game.name,
                    result.game.lifecycle_status.value,
                )

    if failures:
        logger.error("%d of %d game(s) failed", failures, len(appids))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
