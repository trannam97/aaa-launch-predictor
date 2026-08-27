#!/usr/bin/env python3
"""Find releases whose launch began before their Steam store date.

    DATABASE_URL=... python jobs/detect_launch_start.py --dry-run
    DATABASE_URL=... python jobs/detect_launch_start.py

The project's rule is that a launch is the **1.0 version**. A premium edition
unlocking three days early ships 1.0, so the launch began then; an Early Access
period does not ship 1.0, so it does not count. Steam's store date is the
standard edition's, which means the first case is systematically missed — and
missed hardest for the biggest releases, since they are the ones with premium
tiers.

This writes `launch_window_start` where it differs from the store date. The
backfill then measures every window from it, so re-running
`backfill_historical.py --appid ...` afterwards is what actually corrects the
numbers.

Costs two Steam requests per release, plus up to fourteen more for each one
that turns out to have a head start. Run it once, not on a schedule.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.launch_window import detect  # noqa: E402
from app.models import HistoricalRelease, PlatformLaunchType  # noqa: E402
from app.steam import SteamClient, SteamError  # noqa: E402

logger = logging.getLogger("detect_launch_start")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument(
        "--appid", type=int, action="append", dest="appids", help="Only these (repeatable)."
    )
    parser.add_argument(
        "--include-ports",
        action="store_true",
        help="Probe delayed ports and former exclusives too (their windows feed nothing).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    with session_scope() as session:
        # Delayed ports and former exclusives are excluded from labeling and
        # from the cohort index, so nothing reads their windows — probing them
        # spends Steam requests to produce a number no one consumes, and puts
        # rows on the needs-confirming list that are not worth anyone's time.
        # Rise of the Tomb Raider was flagged for a 12-day discrepancy that
        # turned out not to matter: it launched on Xbox in November 2015 and
        # is a port either way. Unknown launch types are still probed, since
        # they may yet resolve to day-one.
        in_scope = {PlatformLaunchType.DAY_ONE_STEAM, PlatformLaunchType.UNKNOWN}
        releases = [
            r
            for r in session.scalars(select(HistoricalRelease))
            if r.steam_release_date
            and (not args.appids or r.steam_appid in args.appids)
            and (args.include_ports or r.platform_launch_type in in_scope)
        ]
        logger.info("Probing %d releases...", len(releases))

        shifted, flagged, early_access, failed = [], [], [], 0
        with SteamClient() as client:
            for release in releases:
                try:
                    result = detect(client, release.steam_appid, release.steam_release_date)
                except SteamError as exc:
                    failed += 1
                    logger.warning("%s: %s", release.game_name[:40], str(exc)[:60])
                    continue
                if "Early Access" in result.reason:
                    early_access.append((release.game_name, result))
                if not result.shifted:
                    continue
                if result.needs_review:
                    flagged.append((release.game_name, result))
                    logger.warning(
                        "%-40s %s -> %s  (%d days) NOT WRITTEN — %s",
                        release.game_name[:40],
                        result.recorded,
                        result.detected,
                        result.days_earlier,
                        result.reason,
                    )
                    continue
                shifted.append((release.game_name, result))
                logger.info(
                    "%-40s %s -> %s  (%d day head start, %s reviews)",
                    release.game_name[:40],
                    result.recorded,
                    result.detected,
                    result.days_earlier,
                    f"{result.head_start_reviews:,}",
                )
                if not args.dry_run:
                    release.launch_window_start = result.detected

        print()
        print(f"  probed                     {len(releases)}")
        print(f"  1.0 on sale before store   {len(shifted)}")
        print(f"  needs confirming, unwritten {len(flagged)}")
        for name, result in flagged:
            print(f"      {name[:40]:42} {result.recorded} -> {result.detected}")
        print(f"  Early Access, date stands  {len(early_access)}")
        for name, _ in early_access:
            print(f"      {name[:52]}")
        if failed:
            print(f"  failed                     {failed}")
        print()

        if args.dry_run:
            logger.info("--dry-run: nothing written.")
            session.rollback()
            return 0

    logger.info(
        "Wrote launch_window_start for %d releases. Re-run backfill_historical.py "
        "for those appids to recompute their windows.",
        len(shifted),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
