#!/usr/bin/env python3
"""Record how anticipated each game was before it shipped.

    DATABASE_URL=... python jobs/enrich_award_nominations.py --dry-run
    DATABASE_URL=... python jobs/enrich_award_nominations.py

Award shows run a category for games that do not exist yet — The Game Awards'
Most Anticipated Game, Golden Joystick's Most Wanted, Gamescom's Most Wanted,
the Japan Game Awards' Future Category. A nomination there is one of the very
few signals in this project that is **verifiably** pre-launch: Wikidata stamps
each nomination with the date it was made, so the ordering can be proven rather
than assumed.

Two things this job is careful about, both of which would silently break the
guarantee that makes the signal worth having:

- **Undated nominations are dropped**, never assumed to precede release.
- **Year-precision dates only count when the whole year precedes the release
  year.** Gamescom and Golden Joystick statements often carry just a year,
  rendered as January 1st; comparing that day directly would count a November
  nomination as preceding a June release.

The cutoff is the *original* release date, not the Steam date — anticipation
is about a game that does not exist anywhere yet, so a console launch ends it
even if the Steam version is years away.

The result is stored, not used. `app/features.py` deliberately does not read
these columns: too few labeled rows carry a nomination for the effect to be
measurable, and adding a feature because it moved a number on thirty rows is
the mistake this project keeps refusing to make. It is captured now because it
can only be captured before launch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease  # noqa: E402
from app.wikidata import ANTICIPATION_AWARDS, WikidataClient, WikidataError  # noqa: E402

logger = logging.getLogger("enrich_award_nominations")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument("--verbose", action="store_true", help="Show every nominated game.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    logger.info("Querying %d award categories on Wikidata...", len(ANTICIPATION_AWARDS))
    with session_scope() as session:
        releases = list(session.scalars(select(HistoricalRelease)))
        try:
            found = WikidataClient().anticipation([r.steam_appid for r in releases])
        except WikidataError as exc:
            logger.error("%s", exc)
            return 1

        by_show: Counter[str] = Counter()
        nominated = 0
        for release in releases:
            record = found.get(release.steam_appid)
            # The cutoff is the game's first existence anywhere. Falling back to
            # the Steam date keeps rows usable before enrichment has run.
            cutoff = release.original_release_date or release.steam_release_date
            qualifying = record.before(cutoff) if record else []
            if qualifying:
                nominated += 1
                for nomination in qualifying:
                    by_show[nomination.award_name] += 1
                if args.verbose:
                    logger.info(
                        "%-40s %d nomination(s), %d win(s)",
                        release.game_name[:40],
                        len(qualifying),
                        sum(n.won for n in qualifying),
                    )
            if not args.dry_run:
                release.prelaunch_award_nominations = len(qualifying)
                release.prelaunch_award_wins = sum(n.won for n in qualifying)

        print()
        print(f"  scanned                 {len(releases)} releases")
        print(f"  with a pre-release nod  {nominated}")
        print()
        for show, count in by_show.most_common():
            print(f"    {count:3}  {show}")
        print()

        if args.dry_run:
            logger.info("--dry-run: nothing written.")
            session.rollback()
            return 0

    logger.info("Wrote nomination counts for %d releases.", len(releases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
