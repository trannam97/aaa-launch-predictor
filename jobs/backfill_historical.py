#!/usr/bin/env python3
"""Phase 0.5: backfill `historical_releases` from the curated research CSV.

Reads the curated CSV, fetches each game's Steam data (structural fields plus
windowed review figures), and upserts the merged row.

    python jobs/backfill_historical.py
    python jobs/backfill_historical.py --csv data/historical_releases.csv
    python jobs/backfill_historical.py --appid 2443720 --dry-run

Re-running is safe and refreshes the API-derived half in place; curated
research is only ever overwritten by the CSV itself, never by Steam.

Requires DATABASE_URL (see .env.example). The Steam endpoints are public.
Exits non-zero if any game failed.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.backfill import (  # noqa: E402
    CuratedCsvError,
    backfill_release,
    load_curated_csv,
)
from app.db import session_scope  # noqa: E402
from app.steam import SteamClient, SteamError  # noqa: E402

DEFAULT_CSV = REPO_ROOT / "data" / "historical_releases.csv"

# Each game costs four Steam requests (details plus three review windows), so
# this is deliberately more conservative than the daily-refresh job's pacing.
DELAY_BETWEEN_GAMES_SECONDS = 2.0

logger = logging.getLogger("backfill_historical")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Curated CSV path.")
    parser.add_argument(
        "--appid",
        type=int,
        action="append",
        dest="appids",
        help="Backfill only these appids from the CSV (repeatable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report without writing to the database.",
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
    # httpx logs a line per request; at four requests a game that buries the
    # per-game progress this job actually reports.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    try:
        curated = load_curated_csv(args.csv)
    except (CuratedCsvError, OSError) as exc:
        logger.error("%s", exc)
        return 2

    if args.appids:
        wanted = set(args.appids)
        missing = wanted - {row.steam_appid for row in curated}
        if missing:
            logger.error("appids not present in %s: %s", args.csv, sorted(missing))
            return 2
        curated = [row for row in curated if row.steam_appid in wanted]

    labeled = sum(1 for row in curated if row.resolved_outcome is not None)
    logger.info(
        "Backfilling %d game(s) from %s — %d labeled, %d awaiting research%s",
        len(curated),
        args.csv,
        labeled,
        len(curated) - labeled,
        " (dry run)" if args.dry_run else "",
    )

    failures = 0
    with session_scope() as session, SteamClient() as client:
        for index, row in enumerate(curated):
            if index:
                time.sleep(args.delay)
            try:
                result = backfill_release(session, row, client)
            except SteamError as exc:
                failures += 1
                logger.error("appid %s (%s) failed: %s", row.steam_appid, row.game_name, exc)
                continue

            for warning in result.warnings:
                logger.warning("appid %s: %s", row.steam_appid, warning)

            launch = next(
                (w for w in result.release.windows if w.window_key.value == "launch_2w"),
                None,
            )
            logger.info(
                "%-9s %-42s %s | 2wk reviews=%s (%s) | label=%s",
                result.release.steam_appid,
                result.release.game_name[:42],
                result.release.steam_release_date,
                launch.review_total if launch else "—",
                f"{launch.positive_pct}%" if launch and launch.positive_pct is not None else "—",
                result.release.resolved_outcome.value if result.release.resolved_outcome else "—",
            )

            if args.dry_run:
                session.expunge_all()
            else:
                session.commit()

        if args.dry_run:
            session.rollback()

    if failures:
        logger.error("%d of %d game(s) failed", failures, len(curated))
        return 1
    logger.info("Done. %d game(s) backfilled.", len(curated) - failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
