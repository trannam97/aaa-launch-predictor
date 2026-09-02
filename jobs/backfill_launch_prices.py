#!/usr/bin/env python3
"""Propose launch prices from IsThereAnyDeal's price history.

    DATABASE_URL=... python jobs/backfill_launch_prices.py --list
    DATABASE_URL=... ITAD_API_KEY=... python jobs/backfill_launch_prices.py --limit 20
    DATABASE_URL=... ITAD_API_KEY=... python jobs/backfill_launch_prices.py --dump-raw shape.json

170 of 206 rows have no `launch_price_usd`, and a missing price does not read as
missing: `app/features.py` turns it into `0.0`, which the model reads as a free
game. Every one of those rows would carry that value into training the moment it
gets a label — which is exactly what the 71-row signal-research queue is about
to do.

Like the signal drafts, this writes a **review file**, never the database and
never `data/historical_releases.csv`. `launch_price_usd` is a curated column,
and the reason it is curated is that the obvious automated answer is wrong.

The `verdict` column does the triage. `launch_price` means ITAD's earliest
record sits within 60 days of release and can be taken at face value.
`too_late` means ITAD only began tracking the title years afterwards, so its
earliest price is a later re-tier, not the launch price — those rows are the
ones worth a human or an LLM pass.

Costs one ITAD request per game — plus a single keyless lookup for the whole
queue — and writes nothing but the review file, so it is safe to re-run.

**Re-running is the point.** ITAD's allowance is small and tiered, and the first
full run spent it after 50 games. So the review file is read back at the start
and games already in it are skipped: run it, wait, run it again, and the file
fills up. Nothing gathered is thrown away when the budget runs out mid-run —
that is what turned a 50-game success into a 120-failure report the first time.
`--refresh` re-asks about rows already proposed.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.itad import (  # noqa: E402
    ItadAuthError,
    ItadClient,
    ItadError,
    ItadRateLimitError,
)
from app.models import HistoricalRelease  # noqa: E402

logger = logging.getLogger("backfill_launch_prices")

DEFAULT_OUT = REPO_ROOT / "data" / "launch_price_proposals.csv"

FIELDS = [
    "steam_appid",
    "game_name",
    "verdict",
    "launch_price_usd",
    "observed_on",
    "days_after_release",
    "steam_release_date",
    "note",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Look up at most this many.")
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
        help="Re-ask about games already in the review file. Off by default so "
        "repeated runs continue the queue rather than re-spending the "
        "allowance on rows that are already answered.",
    )
    parser.add_argument(
        "--dump-raw",
        type=Path,
        help="Write the first raw history response here. The response shape is "
        "not documented publicly; this captures it so the parser can be "
        "corrected in one round rather than guessed at twice.",
    )
    return parser.parse_args(argv)


def needs_price(session) -> list[HistoricalRelease]:
    return list(
        session.scalars(
            select(HistoricalRelease)
            .where(HistoricalRelease.launch_price_cents.is_(None))
            .order_by(HistoricalRelease.steam_release_date)
        )
    )


def read_proposals(path: Path) -> dict[int, dict[str, object]]:
    """Whatever an earlier run already answered, keyed by appid.

    A partial run is the normal case, not the exception: ITAD's allowance runs
    out long before 170 games do. Reading the file back is what makes the next
    run continue instead of repeat.
    """
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


def as_row(appid: int, name: str, released, found) -> dict[str, object]:
    return {
        "steam_appid": appid,
        "game_name": name,
        "verdict": (
            "suspect_shape"
            if found.suspect_shape
            else "launch_price"
            if found.is_launch_price
            else "too_late"
        ),
        "launch_price_usd": f"{found.price_cents / 100:.2f}",
        "observed_on": found.observed_on.isoformat(),
        "days_after_release": found.days_after_release,
        "steam_release_date": released.isoformat() if released else "",
        "note": found.note,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    with session_scope() as session:
        queue = needs_price(session)
        if args.appids:
            queue = [r for r in queue if r.steam_appid in args.appids]
        logger.info("%d release(s) with no launch price", len(queue))
        if args.list_only:
            for release in queue:
                print(f"  {release.steam_appid:<10}{release.game_name}")
            return 0
        pending = [(r.steam_appid, r.game_name, r.steam_release_date) for r in queue]

    proposals = read_proposals(args.out)
    if proposals and not args.refresh:
        before = len(pending)
        pending = [row for row in pending if row[0] not in proposals]
        if before != len(pending):
            logger.info(
                "%d already in %s; continuing with the other %d",
                before - len(pending),
                args.out.name,
                len(pending),
            )

    targets = pending[: args.limit] if args.limit else pending
    if not targets:
        logger.info("Nothing left to look up.")
        return 0

    api_key = os.environ.get("ITAD_API_KEY")
    if not api_key:
        logger.error("ITAD_API_KEY is not set. Register an app at isthereanydeal.com/apps/dev")
        return 1

    unknown = no_history = failed = added = 0
    stopped: str | None = None
    with ItadClient(api_key) as client:
        # One request for every appid, before any of the per-game history calls.
        # Asking per game is what doubled the traffic and spent the allowance.
        try:
            ids = client.lookup_many([appid for appid, _, _ in targets])
        except ItadError as exc:
            logger.error("could not resolve appids: %s", exc)
            return 1
        logger.info("%d of %d appids known to ITAD", len(ids), len(targets))

        dumped = False
        for appid, name, released in targets:
            itad_id = ids.get(appid)
            if itad_id is None:
                unknown += 1
                logger.info("%-40s no ITAD entry for this appid", name[:40])
                continue
            try:
                if args.dump_raw and not dumped:
                    args.dump_raw.write_text(json.dumps(client.history(itad_id), indent=2))
                    logger.info("wrote the raw history for %s to %s", name, args.dump_raw)
                    dumped = True
                found = client.launch_price_for(itad_id, released)
            except (ItadAuthError, ItadRateLimitError) as exc:
                # Neither is per-row: the next 169 games would fail the same
                # way. The first run asked anyway and logged 120 identical
                # warnings. Stop, and keep everything gathered so far.
                logger.error("%s", exc)
                stopped = f"stopped after {added} of {len(targets)}"
                break
            except ItadError as exc:
                failed += 1
                logger.warning("%-40s %s", name[:40], exc)
                continue
            if found is None:
                # Not the same failure as having no ITAD entry, and reporting
                # both as "not tracked" sent the first investigation to the
                # wrong place. /games/history/v2 answers with price *change*
                # events, so a title whose price has not moved inside the
                # window asked about comes back empty -- which is what happens
                # when steam_release_date is NULL and there is no `since` to
                # reach back with.
                no_history += 1
                logger.info(
                    "%-40s ITAD knows it, but returned no price changes%s",
                    name[:40],
                    "" if released else " (no steam_release_date, so no `since`)",
                )
                continue
            proposals[appid] = as_row(appid, name, released, found)
            added += 1
            logger.info("%-40s $%-7s %s", name[:40], f"{found.price_cents / 100:.2f}", found.note)

    # Written even when the run stopped early — the rows are the whole point,
    # and throwing away 50 good ones to report a failure helps nobody.
    write_proposals(args.out, proposals)

    usable = sum(1 for r in proposals.values() if r.get("verdict") == "launch_price")
    suspect = sum(1 for r in proposals.values() if r.get("verdict") == "suspect_shape")
    print()
    if stopped:
        print(f"  {stopped} — re-run to continue")
    print(f"  looked up          {len(targets)}")
    print(f"  new this run       {added}")
    print(f"  in the file        {len(proposals)}")
    print(f"  usable at face     {usable}")
    print(f"  too late to trust  {len(proposals) - usable - suspect}")
    if suspect:
        print(f"  UNRELIABLE shape   {suspect}   <- the parser understood only part of the")
        print("                            response; these numbers mean nothing yet")
    print(f"  no ITAD entry      {unknown}")
    print(f"  no price changes   {no_history}")
    print(f"  failed             {failed}")
    print(f"  written to         {args.out}")
    print()
    print("  Proposals, not values. Copy verified prices into")
    print("  data/historical_releases.csv by hand.")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
