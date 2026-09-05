#!/usr/bin/env python3
"""Propose `platform_launch_type` from the two release dates already on the row.

    DATABASE_URL=... python jobs/backfill_platform_launch_type.py --list
    DATABASE_URL=... python jobs/backfill_platform_launch_type.py
    DATABASE_URL=... python jobs/backfill_platform_launch_type.py --apply

The column exists because a console-first game arriving on Steam a year later
is a different prediction problem: it brings pre-existing reputation and
pent-up demand that a genuine day-one release does not have.

**Against the current database this job has almost nothing to do**, and the
first version of this docstring said otherwise. It claimed 173 of 206 rows were
unset. That count came from `data/historical_releases.csv`, which lags the
database -- the live table holds one UNKNOWN row, Assassin's Creed IV Black
Flag, and that one has no Steam date to compare against. So this runs as a
guard for rows added later, not as a backfill of a gap that exists today.

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

## `--audit`: checking the rows that already have an answer

The default queue is rows holding UNKNOWN, which today is one row. That leaves
the far larger question untouched: are the rows that *do* carry a value right?

The counts above say probably not. 54 rows have their two dates a month or more
apart, and the column named one of them. The other 53 read as day-one launches
while their own dates say Steam got the game between two months and five years
late. That is worth more than a tidy column:

  * `draft_studio_signals.py` researches `day_one_steam` rows, so each wrong one
    is a paid research call on a game whose launch window is anchored wrong.
  * The rubric's headline accuracy is reported over day-one Steam releases only.
    A row that is not really day-one is inside a measurement that says it is.

So `--audit` walks **every** row instead of the UNKNOWN ones, compares the
stored value against what the two dates say, and sorts each row into:

    ok            stored value and dates agree
    conflict      they disagree, or Steam predates the original release
    judgement     3 to 30 days apart -- the dates cannot referee this one
    undecidable   a date is missing
    unset         still UNKNOWN, i.e. the ordinary backfill queue

`--audit` never writes to the database and refuses `--apply` outright. Applying
a date-derived verdict across rows that already hold curated values would
overwrite human answers with arithmetic, which is the inverse of this job's
purpose. Conflicts are for a person to resolve one at a time.

Note the two measurements are not taken the same way: the 54 above was measured
against live Steam listings, while `--audit` compares the two stored columns. If
the counts differ, that gap is itself a finding -- it means `steam_release_date`
has drifted from what Steam serves today.

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

# A separate file, not a mode of the same one. The proposals file persists
# between runs and is read back to skip rows already seen, so folding 206 audit
# rows into it would tell the next backfill run its queue was already done.
DEFAULT_AUDIT_OUT = REPO_ROOT / "data" / "platform_launch_type_audit.csv"

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

# An Early Access graduation has the same shape as a port and the opposite
# answer. Steam reports a graduated title's 1.0 date while original_release_date
# holds the Early Access start, so Grounded reads as 791 days late and Starship
# Troopers: Extermination as 512 -- both marked day_one_steam in the corpus,
# under a launch-is-1.0 rule this job would otherwise contradict. The marker is
# curated in `notes`, so the rows that need the exception announce themselves.
EARLY_ACCESS_MARKER = "EARLY ACCESS GRADUATION"

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

# --audit reports the stored value beside the verdict, so a reviewer can see
# what is being contradicted without opening the database.
AUDIT_FIELDS = [*FIELDS, "stored_launch_type", "agreement"]

OK = "ok"
CONFLICT = "conflict"
JUDGEMENT = "judgement"
UNDECIDABLE = "undecidable"
UNSET = "unset"

# Which stored values each verdict can live with. Only the verdicts that the
# dates actually settle appear here; the rest are handled by name in `audit`.
#
# `early_access` expects day_one_steam rather than a port type on purpose: the
# gap there is the Early Access period and the corpus's launch-is-1.0 rule makes
# the 1.0 date the launch. A row marked delayed_port with an Early Access marker
# is a real disagreement, not an exception to wave through.
VERDICT_ACCEPTS: dict[str, tuple[PlatformLaunchType, ...]] = {
    "day_one_steam": (PlatformLaunchType.DAY_ONE_STEAM,),
    "early_access": (PlatformLaunchType.DAY_ONE_STEAM,),
    "not_day_one": (PlatformLaunchType.DELAYED_PORT, PlatformLaunchType.FORMER_EXCLUSIVE),
}


def display_path(path: Path) -> str:
    """A short path for the log, and never an exception.

    `relative_to` raises when --out points outside the repo, which the workflow
    never does but a person on the command line easily might. That raise landed
    *after* the file was written and, under --apply, after the database write --
    so a run that fully succeeded would exit non-zero and read as a failure.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Look at most this many rows.")
    parser.add_argument(
        "--appid", type=int, action="append", dest="appids", help="Only these (repeatable)."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Review file to write. Defaults to the proposals file, or the audit "
        "file under --audit.",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only", help="Print the queue and exit."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-examine rows already in the review file rather than continuing past them.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Check every row's stored launch type against its two dates instead "
        "of proposing values for the UNKNOWN ones. Never writes the database.",
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


def classify(
    original: date | None, steam: date | None, notes: str | None = None
) -> tuple[str, str, int | None, str]:
    """Return (verdict, proposed launch type, gap in days, note).

    The proposal is empty for everything the dates cannot settle on their own.
    """
    if original is None or steam is None:
        missing = "original_release_date" if original is None else "steam_release_date"
        return "no_date", "", None, f"{missing} is not recorded, so there is nothing to compare"
    gap = (steam - original).days
    if abs(gap) <= DAY_ONE_TOLERANCE_DAYS:
        return "day_one_steam", PlatformLaunchType.DAY_ONE_STEAM.value, gap, ""
    if notes and EARLY_ACCESS_MARKER in notes:
        return (
            "early_access",
            "",
            gap,
            f"The {gap}-day gap is the Early Access period, not a port: "
            "original_release_date is the Early Access start and Steam reports "
            "the 1.0 date. Under the corpus's launch-is-1.0 rule this is a "
            f"{PlatformLaunchType.DAY_ONE_STEAM.value} release",
        )
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
        "storefront first -- the dates cannot tell these apart. Check for an "
        "Early Access graduation too: there the gap is the Early Access period "
        f"and the row is {PlatformLaunchType.DAY_ONE_STEAM.value}",
    )


def audit(stored: PlatformLaunchType | None, verdict: str) -> tuple[str, str]:
    """Compare a stored launch type against what the two dates say.

    Returns (agreement, why). `why` is filled in only when there is something
    to explain -- an agreeing row needs no prose.

    The asymmetry to keep in mind: agreeing does not make the stored value
    right, it only means the dates raise no objection. `former_exclusive` and
    `delayed_port` are interchangeable as far as this check can see, so a row
    with the wrong one of those two reads as `ok` here. This finds rows the
    dates *contradict*, which is a smaller claim than rows that are wrong.
    """
    if verdict == "no_date":
        return UNDECIDABLE, "one of the two dates is missing, so nothing can be checked"
    if verdict == "steam_first":
        # Independent of the column: original_release_date is the earliest
        # publication anywhere, so Steam cannot precede it. A stored value
        # resting on these dates rests on a broken pair.
        return (
            CONFLICT,
            "Steam predates the curated original release -- one of the two dates is wrong",
        )
    if stored is None or stored == PlatformLaunchType.UNKNOWN:
        return UNSET, "no stored value yet -- this row is the ordinary backfill queue"
    if verdict == "near_day_one":
        # 3 to 30 days. Whether a console-first stagger still counts as day-one
        # is a decision about the launch, so any stored value here is defensible
        # and calling it a conflict would bury the real ones in noise.
        return JUDGEMENT, f"stored {stored.value}; the dates are too close to referee it"
    accepted = VERDICT_ACCEPTS[verdict]
    if stored in accepted:
        return OK, ""
    return CONFLICT, (
        f"stored {stored.value}, but the dates read as {verdict} -- expected "
        + " or ".join(value.value for value in accepted)
    )


def all_rows(session) -> list[HistoricalRelease]:
    """Every row, for --audit.

    Deliberately unfiltered: the stored launch type is the thing being checked,
    so filtering on it would hide exactly the rows worth finding.
    """
    return list(
        session.scalars(select(HistoricalRelease).order_by(HistoricalRelease.steam_release_date))
    )


def as_audit_row(release: HistoricalRelease) -> dict[str, object]:
    row = as_row(release)
    stored = release.platform_launch_type
    agreement, why = audit(stored, str(row["verdict"]))
    row["stored_launch_type"] = stored.value if stored else ""
    row["agreement"] = agreement
    # The disagreement is the point of the row, so it leads the note.
    row["note"] = ". ".join(part for part in (why, str(row["note"])) if part)
    return row


def as_row(release: HistoricalRelease) -> dict[str, object]:
    verdict, proposed, gap, note = classify(
        release.original_release_date, release.steam_release_date, release.notes
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


def write_proposals(
    path: Path, rows: dict[int, dict[str, object]], fields: list[str] | None = None
) -> None:
    fields = fields or FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for appid in sorted(rows):
            writer.writerow({field: rows[appid].get(field, "") for field in fields})


def run_audit(session, args) -> int:
    """Check stored launch types against the dates. Reads only."""
    queue = all_rows(session)
    if args.appids:
        wanted = set(args.appids)
        queue = [r for r in queue if r.steam_appid in wanted]
    if args.limit is not None:
        queue = queue[: args.limit]

    rows = {r.steam_appid: as_audit_row(r) for r in queue}
    write_proposals(args.out, rows, AUDIT_FIELDS)

    counts: dict[str, int] = {}
    for row in rows.values():
        agreement = str(row["agreement"])
        counts[agreement] = counts.get(agreement, 0) + 1

    conflicts = [row for row in rows.values() if row["agreement"] == CONFLICT]
    for row in sorted(conflicts, key=lambda r: -abs(int(r["gap_days"] or 0))):
        print(
            f"  {row['steam_appid']:<10}{str(row['game_name'])[:44]:<46}"
            f"{row['stored_launch_type']:<18}{row['gap_days']:>7}d"
        )
    if conflicts:
        print()

    for agreement in sorted(counts):
        print(f"  {counts[agreement]:>4}  {agreement}")
    print(f"\n  Wrote {display_path(args.out)}")

    # The two numbers a reader needs to act on, spelled out rather than left to
    # be inferred from the table: what is wrong, and what it is costing.
    # Two different findings, kept apart. A day-one row with a port-sized gap is
    # a mislabelled row; a steam_first row is a broken date pair, and since one
    # of its dates is wrong there is no telling whether its column is. Counting
    # them together would overstate the first and hide the second.
    mislabelled = [
        row
        for row in conflicts
        if row["stored_launch_type"] == PlatformLaunchType.DAY_ONE_STEAM.value
        and row["verdict"] == "not_day_one"
    ]
    broken_dates = [row for row in conflicts if row["verdict"] == "steam_first"]

    if mislabelled:
        print(
            f"\n  {len(mislabelled)} row(s) stored as "
            f"{PlatformLaunchType.DAY_ONE_STEAM.value} have a gap of "
            f"{PORT_TOLERANCE_DAYS}+ days."
        )
        print(
            "  Each is eligible for paid signal research (~$0.21 batched) on a "
            "launch window\n  anchored to the wrong date, and sits inside a rubric "
            "figure reported over\n  day-one releases only."
        )
    if broken_dates:
        print(
            f"\n  {len(broken_dates)} row(s) have Steam predating the original "
            "release. That is a\n  data error, not a launch type -- fix the dates "
            "before reading the column."
        )
    print("\n  Wrote nothing to the database. --audit never does.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.out is None:
        args.out = DEFAULT_AUDIT_OUT if args.audit else DEFAULT_OUT

    if args.audit and args.apply:
        # Not a warning. In audit mode the queue is every row, so --apply would
        # stamp day_one_steam over rows already holding a curated delayed_port
        # or former_exclusive -- overwriting human answers with arithmetic.
        print("  --apply cannot be combined with --audit: the audit reads rows that")
        print("  already hold curated values, and would overwrite them with a guess.")
        return 2

    if args.audit:
        with session_scope() as session:
            return run_audit(session, args)

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
        print(f"\n  Wrote {display_path(args.out)}")
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
