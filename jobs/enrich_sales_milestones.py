#!/usr/bin/env python3
"""Collect publisher-announced units-sold figures from Wikidata, for review.

    DATABASE_URL=... python jobs/enrich_sales_milestones.py --list
    DATABASE_URL=... python jobs/enrich_sales_milestones.py

The rubric decides Flop from Success on Steam review counts and scores, because
those are the only launch-window quantities recoverable for a game released
years ago — Steam's review endpoint takes a date range, and nothing else does.
Units, revenue and concurrent players are all *now* numbers.

Wikidata P2664 is the exception. Publishers announce milestones ("2 million
sold"), those announcements enter Wikidata carrying a P585 point in time, and a
dated figure close to a launch is a launch-window fact rather than a lifetime
snapshot. Coverage when this was written: 92 of the 206 corpus rows, and 24 of
the 68 rows in the signal-drafts queue, 13 of those dated within 120 days of the
Steam release.

**This job never writes the database or the curated CSV.** It writes a review
file, like the price and signal jobs, for three reasons that are not
incidental.

## Why the figures cannot be a rubric input

**They are worldwide, across every platform.** They answer "did the game sell",
never "did the Steam launch sell". TEKKEN 8's 2 million at 21 days is every
platform; its Steam share is unknown and unknowable from here.

**They exist because a publisher chose to announce them.** That is what
publishers do when a game does well, so the source supplies evidence for
successes and silence for failures. Feeding it to the rubric would let it
upgrade rows and never downgrade one.

**Absence means two different things at the two ends of the range**, which is
the finding that matters most here. Share of the 68-row queue carrying a figure,
by launch sentiment:

    under 50%      1/8    12%
    50-59%         2/7    29%
    60-69%         8/14   57%
    70-77%         9/20   45%
    78%+           5/19   26%

Not monotonic. Below 50% the game did badly and there was nothing to announce.
Above 78% — the rows that reviewed well and failed only the volume floor — the
gap is *segment*: day-one Game Pass titles (Grounded, High On Life, Indiana
Jones), niche PC genres (Pillars of Eternity II, Wasteland 3) and remasters
(Mass Effect Legendary Edition, Beyond Good & Evil 20th) rarely get a unit
milestone however well they did. Only 5 of 18 volume-floor failures carry one.

So a missing figure is not evidence of a weak launch, and reading it that way
would import the same segment confound the volume floor already has (see
PROJECT_SPEC, "The Volume Floor Conflates Market Segment With Commercial
Failure").

## What it is for

Giving a reviewer the same evidence on every row, rather than on whichever rows
someone happened to know about. TEKKEN 8 was labelled `success` against the
rubric on sales figures supplied from memory; Monster Hunter Wilds sold 8 million
in five days and sat unlabelled in the queue because nobody looked it up. That
asymmetry is a selection effect in the corpus, and a systematic pass is the fix.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease  # noqa: E402
from app.wikidata import SalesHistory, WikidataClient, WikidataError  # noqa: E402

logger = logging.getLogger("enrich_sales_milestones")

DEFAULT_OUT = REPO_ROOT / "data" / "sales_milestones.csv"

# Past this, a figure describes a long tail rather than a launch. Not a
# threshold the job enforces -- every dated milestone is written out and the
# distance is a column -- but the count it reports, so "how many rows did this
# actually inform" is answerable without opening the file.
LAUNCH_WINDOW_DAYS = 120

FIELDS = [
    "steam_appid",
    "game_name",
    "steam_release_date",
    "units_at_launch",
    "as_of",
    "days_after_launch",
    "within_launch_window",
    "all_milestones",
    "note",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Look up at most this many rows.")
    parser.add_argument(
        "--appid", type=int, action="append", dest="appids", help="Only these (repeatable)."
    )
    parser.add_argument("--out", type=Path, default=None, help="Review file to write.")
    parser.add_argument(
        "--list", action="store_true", dest="list_only", help="Print the queue and exit."
    )
    return parser.parse_args(argv)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def queue(session, appids: list[int] | None, limit: int | None) -> list[HistoricalRelease]:
    """Every row with a Steam date, since the date is what makes a figure readable.

    Unlike the signal-drafts queue this is not restricted to unlabelled or
    day-one rows: a labelled row's sales figure is evidence about a label that
    already exists, which is exactly what a reviewer checking one wants.
    """
    rows = list(
        session.scalars(
            select(HistoricalRelease)
            .where(HistoricalRelease.steam_release_date.is_not(None))
            .order_by(HistoricalRelease.steam_release_date)
        )
    )
    if appids:
        wanted = set(appids)
        rows = [r for r in rows if r.steam_appid in wanted]
    return rows[:limit] if limit is not None else rows


def as_row(release: HistoricalRelease, history: SalesHistory | None) -> dict[str, object]:
    launch = release.steam_release_date
    first = history.first_after(launch) if history else None
    gap = (first.as_of - launch).days if first and first.as_of and launch else None
    undated = sum(1 for m in history.milestones if not m.dated) if history else 0
    if history is None:
        note = (
            "no units-sold figure in Wikidata -- see the module docstring: "
            "absence is not evidence of a weak launch"
        )
    elif first is None:
        note = f"{len(history.milestones)} milestone(s), none dated on or after the Steam release"
    elif gap is not None and gap > LAUNCH_WINDOW_DAYS:
        note = f"earliest figure is {gap} days out -- a long tail, not a launch"
    else:
        note = "worldwide across all platforms, not Steam-specific"
    if undated:
        note += f"; {undated} undated milestone(s) also held"
    return {
        "steam_appid": release.steam_appid,
        "game_name": release.game_name,
        "steam_release_date": launch.isoformat() if launch else "",
        "units_at_launch": first.units if first else "",
        "as_of": first.as_of.isoformat() if first and first.as_of else "",
        "days_after_launch": "" if gap is None else gap,
        "within_launch_window": (
            "" if gap is None else ("yes" if gap <= LAUNCH_WINDOW_DAYS else "no")
        ),
        "all_milestones": " | ".join(
            f"{m.units:,} as of {m.as_of.isoformat() if m.as_of else 'undated'}"
            for m in (history.milestones if history else [])
        ),
        "note": note,
    }


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = args.out or DEFAULT_OUT

    with session_scope() as session:
        rows = queue(session, args.appids, args.limit)

        if args.list_only:
            for release in rows:
                print(f"  {release.steam_appid:<10}{release.game_name[:44]}")
            print()
            print(f"  {len(rows)} row(s) would be looked up on Wikidata.")
            print("  One SPARQL join per batch. No API key, no cost, nothing written.")
            return 0

        try:
            found = WikidataClient().sales_milestones([r.steam_appid for r in rows])
        except WikidataError as exc:
            logger.error("  Wikidata lookup failed: %s", exc)
            return 1

        written = [as_row(r, found.get(r.steam_appid)) for r in rows]
        write_rows(out, written)

        dated = [r for r in written if r["days_after_launch"] != ""]
        near = [r for r in written if r["within_launch_window"] == "yes"]
        print(f"  {len(rows):>4}  rows looked up")
        print(f"  {len(found):>4}  carry at least one units-sold figure")
        print(f"  {len(dated):>4}  have one dated on or after the Steam release")
        print(f"  {len(near):>4}  of those land within {LAUNCH_WINDOW_DAYS} days of it")
        print(f"\n  Wrote {display_path(out)}")
        print("  Nothing was written to the database, and nothing here is a label.")
        print("  Figures are worldwide across all platforms and exist because a publisher")
        print("  chose to announce them, so a missing one is not evidence of a weak launch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
