#!/usr/bin/env python3
"""Fill `original_release_date` from Wikidata, so launch types stop being unknown.

    python jobs/enrich_release_dates.py --dry-run
    python jobs/enrich_release_dates.py

Steam knows when a game arrived on Steam and nothing about the console launch
that may have preceded it. Without the original date,
`derive_platform_launch_type` has nothing to compare against, so a row falls to
`unknown` and is excluded from labeling and from training. That was the state of
166 of the corpus's 205 rows — not for want of outcome research, but for want of
one date.

This writes only `original_release_date`, never `platform_launch_type`. The
day-one-versus-port rule stays in `app/backfill.py` as the single place that
decides it; this job supplies the input and lets that rule run.

Rows are skipped, not guessed, when Wikidata has no match, when it holds only a
year-precision date, or when the Steam date precedes the earliest known release
by more than the tolerance — that last case means one of the two sources is
wrong, and inventing a classification would bury the problem.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.backfill import DAY_ONE_TOLERANCE_DAYS, load_curated_csv  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease  # noqa: E402
from app.wikidata import WikidataClient, WikidataError  # noqa: E402

DEFAULT_CSV = REPO_ROOT / "data" / "historical_releases.csv"
PROVENANCE = "Wikidata P577 (earliest publication date, joined on P1733)"

logger = logging.getLogger("enrich_release_dates")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Curated CSV path.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing."
    )
    parser.add_argument("--verbose", action="store_true", help="Show every row.")
    return parser.parse_args(argv)


def classify(original: date, steam: date | None) -> str:
    """What the existing rule will make of this pair, for reporting only."""
    if steam is None:
        return "not yet backfilled"
    gap = (steam - original).days
    if gap < -DAY_ONE_TOLERANCE_DAYS:
        return "suspect"
    if abs(gap) <= DAY_ONE_TOLERANCE_DAYS:
        return "day_one_steam"
    return "delayed_port"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    curated = load_curated_csv(args.csv)
    wanted = [row for row in curated if row.original_release_date is None]
    if not wanted:
        logger.info("Every row already carries an original_release_date.")
        return 0

    logger.info("Looking up %d appids on Wikidata...", len(wanted))
    try:
        found = WikidataClient().release_dates([row.steam_appid for row in wanted])
    except WikidataError as exc:
        logger.error("%s", exc)
        return 1

    # Steam's own release date lives in the database, not the curated CSV —
    # it is fetched, not researched. Rows not yet backfilled simply have none,
    # and are written without the sanity check rather than skipped.
    with session_scope() as session:
        steam_dates = {
            row.steam_appid: row.steam_release_date
            for row in session.scalars(select(HistoricalRelease))
        }
    updates: dict[int, date] = {}
    counts = {"day_one_steam": 0, "delayed_port": 0, "suspect": 0, "not yet backfilled": 0}
    unmatched: list[str] = []

    for row in wanted:
        record = found.get(row.steam_appid)
        earliest = record.earliest if record else None
        if earliest is None:
            unmatched.append(f"{row.steam_appid} {row.game_name}")
            continue
        verdict = classify(earliest, steam_dates.get(row.steam_appid))
        counts[verdict] += 1
        if verdict == "suspect":
            logger.warning(
                "%s (%s): Steam %s precedes earliest known release %s — skipped",
                row.game_name,
                row.steam_appid,
                steam_dates.get(row.steam_appid),
                earliest,
            )
            continue
        updates[row.steam_appid] = earliest
        if args.verbose:
            logger.info("%-42s %s  %s", row.game_name[:42], earliest, verdict)

    print()
    print(f"  looked up          {len(wanted)}")
    print(f"  matched            {len(wanted) - len(unmatched)}")
    for key, value in counts.items():
        if value:
            print(f"    {key:<16} {value}")
    print(f"  no Wikidata match  {len(unmatched)}")
    for entry in unmatched:
        print(f"    {entry}")
    print()

    if args.dry_run:
        logger.info("--dry-run: nothing written.")
        return 0
    if not updates:
        logger.info("Nothing to write.")
        return 0

    _write_csv(args.csv, updates)
    logger.info("Wrote original_release_date for %d rows to %s", len(updates), args.csv)
    return 0


def _write_csv(path: Path, updates: dict[int, date]) -> None:
    """Rewrite the CSV in place, touching only the two columns we own."""
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0].keys())

    for row in rows:
        appid = int(row["steam_appid"])
        if appid not in updates:
            continue
        row["original_release_date"] = updates[appid].isoformat()
        existing = (row.get("sources") or "").strip()
        if PROVENANCE not in existing:
            row["sources"] = f"{existing} / {PROVENANCE}".strip(" /")

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
