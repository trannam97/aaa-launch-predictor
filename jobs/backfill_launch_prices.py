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

Costs two ITAD requests per game and writes nothing, so it is safe to re-run.
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
from app.itad import ItadClient, ItadError  # noqa: E402
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
        targets = [
            (r.steam_appid, r.game_name, r.steam_release_date)
            for r in (queue[: args.limit] if args.limit else queue)
        ]

    if not targets:
        logger.info("Nothing to look up.")
        return 0

    api_key = os.environ.get("ITAD_API_KEY")
    if not api_key:
        logger.error("ITAD_API_KEY is not set. Register an app at isthereanydeal.com/apps/dev")
        return 1

    rows: list[dict[str, object]] = []
    missing = failed = 0
    dumped = False
    with ItadClient(api_key) as client:
        for appid, name, released in targets:
            try:
                if args.dump_raw and not dumped:
                    itad_id = client.lookup(appid)
                    if itad_id is not None:
                        args.dump_raw.write_text(json.dumps(client.history(itad_id), indent=2))
                        logger.info("wrote the raw history for %s to %s", name, args.dump_raw)
                        dumped = True
                found = client.launch_price(appid, released)
            except ItadError as exc:
                failed += 1
                logger.warning("%-40s %s", name[:40], exc)
                continue
            if found is None:
                missing += 1
                logger.info("%-40s not tracked by ITAD", name[:40])
                continue
            rows.append(
                {
                    "steam_appid": appid,
                    "game_name": name,
                    "verdict": "launch_price" if found.is_launch_price else "too_late",
                    "launch_price_usd": f"{found.price_cents / 100:.2f}",
                    "observed_on": found.observed_on.isoformat(),
                    "days_after_release": found.days_after_release,
                    "steam_release_date": released.isoformat() if released else "",
                    "note": found.note,
                }
            )
            logger.info("%-40s $%-7s %s", name[:40], f"{found.price_cents / 100:.2f}", found.note)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    usable = sum(1 for r in rows if r["verdict"] == "launch_price")
    print()
    print(f"  looked up          {len(targets)}")
    print(f"  usable at face     {usable}")
    print(f"  too late to trust  {len(rows) - usable}")
    print(f"  not tracked        {missing}")
    print(f"  failed             {failed}")
    print(f"  written to         {args.out}")
    print()
    print("  Proposals, not values. Copy verified prices into")
    print("  data/historical_releases.csv by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
