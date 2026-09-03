#!/usr/bin/env python3
"""Propose `platform_launch_type` from the two release dates already on the row.

    DATABASE_URL=... python jobs/backfill_platform_launch_type.py --list
    DATABASE_URL=... python jobs/backfill_platform_launch_type.py
    DATABASE_URL=... python jobs/backfill_platform_launch_type.py --apply

173 of 206 rows carry no launch type, and the column exists precisely because a
console-first game arriving on Steam a year later is a different prediction
problem: it brings pre-existing reputation and pent-up demand that a genuine
day-one release does not have.

The gap is not hypothetical. Comparing both dates across all 206 rows against
live Steam listings: 142 agree within a day, 9 differ by 3 to 26 days, and 54
differ by a month or more -- 49 of those by over half a year. Halo: The Master
Chief Collection reaches Steam 1848 days after its Xbox launch, Diablo II:
Resurrected 1602 days after leaving Battle.net exclusivity. The column named
exactly one of the 54. Every other one reads as a day-one launch.

Nothing here needs the network. `HistoricalRelease` already stores both dates:
`steam_release_date` from Steam's own appdetails, `original_release_date`
curated from Wikidata's earliest publication date. The launch type is the
relationship between them, so this is arithmetic on data already gathered.

**What the dates can and cannot settle.** They settle day-one only when they
agree to within a timezone. They cannot separate `delayed_port` from
`former_exclusive` -- both look identical as a gap, and telling them apart means
knowing whether the earlier platform was a console or a rival PC storefront. Nor
can they say whether a two-week console-first stagger counts as day-one at all.

So there are four verdicts and only one of them proposes a value:

    day_one_steam   within a day of each other; decided
    near_day_one    3 to 30 days apart; real, but a judgement about the launch
    not_day_one     a month or more; pick delayed_port or former_exclusive
    no_date         one of the two dates is missing

Guessing at the other three would put an invented value in a curated column,
which is the failure this job exists to correct.

Like the price and signal jobs this writes a **review file** by default and
touches neither the database nor `data/historical_releases.csv`. `--apply`
writes the `day_one_steam` verdicts only, never the ambiguous ones: for those
rows the two dates agreeing *is* the definition, not a judgement call.

## Applying this costs money later

`jobs/draft_studio_signals.py` researches `day_one_steam` rows and skips every
other launch type, so this column is the gate on that queue. A row moved from
UNKNOWN to `day_one_steam` becomes eligible for a paid research call -- $0.34
synchronous, about $0.21 batched, at the rate measured over 74 rows.

So `--apply` does not just tidy a column; it can enlarge the next research bill.
Run `python jobs/draft_studio_signals.py --list` afterwards, which is free, and
compare the queue against what it was before deciding to spend.
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

from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease, PlatformLaunchType  # noqa: E402

logger = logging.getLogger("backfill_platform_launch_type")

DEFAULT_OUT = REPO_ROOT / "data" / "platform_launch_type_proposals.csv"

# Steam stores a single date for a worldwide release, so a launch crossing
# timezones lands a day either side of the curated date. That is the only gap
# this job will call day-one on its own.
#
# It is tempting to allow more, and the corpus shows why not. The gaps do not
# fall into two clean clusters: 142 rows sit within a day, then 9 spread across
# 3 to 26 days, then 54 run from 58 days to five years. The middle band is real
# console-first staggering -- Watch_Dogs 2 at 13 days, NieR:Automata at 22,
# Assassin's Creed Syndicate at 26 -- where Steam buyers arrived after a review
# cycle had already happened elsewhere. Whether that counts as day-one is a
# judgement about the launch, not a fact about the dates, so it goes to review.
DAY_ONE_TOLERANCE_DAYS = 1

# Past this the gap is unambiguous. Between the two thresholds a row is real but
# undecided, which is its own verdict rather than a coin flip either way.
PORT_TOLERANCE_DAYS = 30

FIELDS = [
    "steam_appid",
    "game_name",
    "verdict",
    "platform_launch_type",
    "original_release_date",
    "steam_release_date",
    "gap_days",
    "note",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Look at most this many rows.")
    parser.add_argument(
        "--appid", type=int, action="append", dest="appids", help="Only these (repeatable)."
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Review file to write.")
    parser.add_argument(
        "--list", action="store_true", dest="list_only", help="Print the queue and exit."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-examine rows already in the review file rather than continuing past them.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the day_one_steam verdicts to the database. Ambiguous rows "
        "are never written, whatever this is set to.",
    )
    return parser.parse_args(argv)


def needs_launch_type(session) -> list[HistoricalRelease]:
    """Rows with no launch type yet.

    `platform_launch_type` is `nullable=False, default=UNKNOWN`, so an
    unanswered row holds UNKNOWN and not NULL -- `.is_(None)` matches nothing
    and a query written that way silently returns the whole corpus. That
    mistake cost a $25 research run on this same schema.
    """
    return list(
        session.scalars(
            select(HistoricalRelease)
            .where(HistoricalRelease.platform_launch_type == PlatformLaunchType.UNKNOWN)
            .order_by(HistoricalRelease.steam_release_date)
        )
    )


def classify(original: date | None, steam: date | None) -> tuple[str, str, int | None, str]:
    """Return (verdict, proposed launch type, gap in days, note).

    The proposal is empty for everything the dates cannot settle on their own.
    """
    if original is None or steam is None:
        missing = "original_release_date" if original is None else "steam_release_date"
        return "no_date", "", None, f"{missing} is not recorded, so there is nothing to compare"
    gap = (steam - original).days
    if abs(gap) <= DAY_ONE_TOLERANCE_DAYS:
        return "day_one_steam", PlatformLaunchType.DAY_ONE_STEAM.value, gap, ""
    if 0 < gap <= PORT_TOLERANCE_DAYS:
        return (
            "near_day_one",
            "",
            gap,
            f"Steam listing arrives {gap} days later — too far apart to be a "
            "timezone, too close to be a port. Decide whether buyers arrived "
            "after a review cycle elsewhere",
        )
    if gap < 0:
        return (
            "steam_first",
            "",
            gap,
            "Steam predates the curated original release, which should not happen -- "
            "one of the two dates is wrong",
        )
    return (
        "not_day_one",
        "",
        gap,
        f"Steam listing arrives {gap} days later. Pick "
        f"{PlatformLaunchType.DELAYED_PORT.value} if the game was console-first, "
        f"{PlatformLaunchType.FORMER_EXCLUSIVE.value} if it was on another PC "
        "storefront first -- the dates cannot tell these apart",
    )


def as_row(release: HistoricalRelease) -> dict[str, object]:
    verdict, proposed, gap, note = classify(
        release.original_release_date, release.steam_release_date
    )
    return {
        "steam_appid": release.steam_appid,
        "game_name": release.game_name,
        "verdict": verdict,
        "platform_launch_type": proposed,
        "original_release_date": (
            release.original_release_date.isoformat() if release.original_release_date else ""
        ),
        "steam_release_date": (
            release.steam_release_date.isoformat() if release.steam_release_date else ""
        ),
        "gap_days": "" if gap is None else gap,
        "note": note,
    }


def read_proposals(path: Path) -> dict[int, dict[str, object]]:
    if not path.exists():
        return {}
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    kept: dict[int, dict[str, object]] = {}
    for row in rows:
        try:
            kept[int(row["steam_appid"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return kept


def write_proposals(path: Path, rows: dict[int, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for appid in sorted(rows):
            writer.writerow({field: rows[appid].get(field, "") for field in FIELDS})


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    existing = {} if args.refresh else read_proposals(args.out)

    with session_scope() as session:
        queue = needs_launch_type(session)
        if args.appids:
            wanted = set(args.appids)
            queue = [r for r in queue if r.steam_appid in wanted]
        if not args.refresh:
            queue = [r for r in queue if r.steam_appid not in existing]
        if args.limit is not None:
            queue = queue[: args.limit]

        if args.list_only:
            for release in queue:
                print(f"  {release.steam_appid:<10}{release.game_name}")
            # After the rows: the workflow shows a tail of this log, so a count
            # printed first is the first thing cut.
            print()
            print(f"  {len(queue)} row(s) would be examined.")
            print("  Reads two columns already on the row. No network, no cost.")
            return 0

        proposals = dict(existing)
        counts: dict[str, int] = {}
        applied = 0
        for release in queue:
            row = as_row(release)
            proposals[release.steam_appid] = row
            verdict = str(row["verdict"])
            counts[verdict] = counts.get(verdict, 0) + 1
            if args.apply and verdict == "day_one_steam":
                release.platform_launch_type = PlatformLaunchType.DAY_ONE_STEAM
                applied += 1
            write_proposals(args.out, proposals)

        for verdict in sorted(counts):
            print(f"  {counts[verdict]:>4}  {verdict}")
        print(f"\n  Wrote {args.out.relative_to(REPO_ROOT)}")
        if args.apply:
            print(f"  Applied day_one_steam to {applied} row(s).")
            # Not a footnote: this column gates the signal-drafts queue, so
            # these rows just became billable research.
            print(
                f"  Up to {applied} row(s) are now eligible for signal research "
                f"(~${applied * 0.21:.0f} batched, ~${applied * 0.34:.0f} synchronous)."
            )
            print("  Run `draft_studio_signals.py --list` for the real queue. It is free.")
        else:
            ambiguous = counts.get("not_day_one", 0) + counts.get("steam_first", 0)
            print(f"  Wrote nothing to the database. {ambiguous} row(s) need a human decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
