#!/usr/bin/env python3
"""Draft studio and support signals for the rows the rubric refuses to call.

    DATABASE_URL=... ANTHROPIC_API_KEY=... python jobs/draft_studio_signals.py --list
    DATABASE_URL=... ANTHROPIC_API_KEY=... python jobs/draft_studio_signals.py --limit 5

The rubric splits Success from Breakout on its own, from Steam data. It refuses
to split Flop from Underperform without a studio-outcome or post-launch-support
signal, and neither exists in any Steam field. That refusal is what leaves 71
day-one releases unlabeled while the upper tiers label themselves.

This researches those two values with Claude and web search and writes them to
a **review file**. It never writes the database and never edits
`data/historical_releases.csv`. A draft is not a label: a human opens the
sources, and only then does the value move into the curated CSV.

Rows are selected by running the real rubric over unlabeled day-one releases and
keeping the ones it cannot resolve — so nothing is researched whose signals the
rubric would not have read anyway.

Costs one Claude call with web search per game. Start with --limit.
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

from app.cohort import CohortIndex  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import (  # noqa: E402
    HistoricalRelease,
    PlatformLaunchType,
    ReleaseWindow,
    WindowKey,
)
from app.research import ResearchError, ResearchTarget, draft_signals  # noqa: E402
from app.rubric import RubricInput, classify  # noqa: E402

logger = logging.getLogger("draft_studio_signals")

DEFAULT_OUT = REPO_ROOT / "data" / "signal_drafts.csv"

FIELDS = [
    "steam_appid",
    "game_name",
    "needs_attention",
    "studio_signal",
    "support_signal",
    "confidence",
    "sources",
    "studio_evidence",
    "support_evidence",
    "reviewer_note",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=5, help="Research at most this many (default: %(default)s)."
    )
    parser.add_argument(
        "--appid", type=int, action="append", dest="appids", help="Only these (repeatable)."
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Review file to write.")
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="Print the queue and exit. Calls no API and costs nothing.",
    )
    return parser.parse_args(argv)


def candidates(session) -> list[HistoricalRelease]:
    """Unlabeled day-one releases the rubric cannot resolve.

    Deliberately not every unlabeled row: about 40% of them meet expectations
    and get a tier from Steam data alone, so researching those would spend a
    call to learn something the rubric never reads.
    """
    index = CohortIndex.from_db(session)
    windows = {
        w.release_id: w
        for w in session.scalars(
            select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_2W)
        )
    }
    late = {
        w.release_id: w
        for w in session.scalars(
            select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_3M)
        )
    }

    queue: list[HistoricalRelease] = []
    for release in session.scalars(
        select(HistoricalRelease)
        .where(HistoricalRelease.resolved_outcome.is_(None))
        .order_by(HistoricalRelease.steam_release_date)
    ):
        if release.platform_launch_type is not PlatformLaunchType.DAY_ONE_STEAM:
            continue
        window = windows.get(release.id)
        if window is None or window.review_total is None:
            continue
        percentile, stats = index.percentile(release.cohort_year, window.review_total)
        tail = late.get(release.id)
        retention = (
            tail.review_total / window.review_total
            if tail and tail.review_total and window.review_total
            else None
        )
        result = classify(
            RubricInput(
                volume_percentile=percentile,
                positive_pct=window.positive_pct,
                retention_ratio=retention,
                studio_signal=release.studio_signal,
                support_signal=release.support_signal,
                cohort_reliable=stats.is_reliable,
            )
        )
        if not result.resolved:
            queue.append(release)
    return queue


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    with session_scope() as session:
        queue = candidates(session)
        if args.appids:
            queue = [r for r in queue if r.steam_appid in args.appids]
        logger.info("%d release(s) the rubric cannot resolve without signals", len(queue))

        if args.list_only:
            for release in queue:
                print(f"  {release.steam_appid:<10}{release.game_name}")
            return 0

        targets = [
            (
                r.steam_appid,
                ResearchTarget(
                    game_name=r.game_name,
                    developer=r.developer,
                    publisher=r.publisher,
                    steam_release_date=r.steam_release_date,
                ),
            )
            for r in queue[: args.limit]
        ]

    if not targets:
        logger.info("Nothing to research.")
        return 0

    import anthropic  # noqa: PLC0415 — only needed on the path that calls the API

    client = anthropic.Anthropic()
    rows, failed = [], 0
    for appid, target in targets:
        try:
            draft = draft_signals(client.beta.messages, target)
        except ResearchError as exc:
            failed += 1
            logger.warning("%s", exc)
            continue
        flags = draft.review_flags
        rows.append(
            {
                "steam_appid": appid,
                "game_name": target.game_name,
                "needs_attention": flags,
                "studio_signal": draft.studio_signal,
                "support_signal": draft.support_signal,
                "confidence": draft.confidence,
                "sources": " | ".join(draft.sources),
                "studio_evidence": draft.studio_evidence,
                "support_evidence": draft.support_evidence,
                "reviewer_note": draft.reviewer_note,
            }
        )
        logger.info(
            "%-40s studio=%-15s support=%-10s %s",
            target.game_name[:40],
            draft.studio_signal,
            draft.support_signal,
            flags,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print()
    print(f"  drafted            {len(rows)}")
    print(f"  failed             {failed}")
    print(f"  need attention     {sum(1 for r in rows if r['needs_attention'])}")
    print(f"  written to         {args.out}")
    print()
    print("  These are drafts, not labels. Open the sources, then copy verified")
    print("  values into data/historical_releases.csv by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
