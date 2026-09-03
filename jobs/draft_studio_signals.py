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

## Two paths, and when to use which

    --limit 5                     research 5 now, synchronously
    --batch                       queue the whole run, print a batch id, exit
    --collect BATCH_ID            write the review file from a finished batch

The synchronous path answers immediately and is the one to smoke-test with. The
batch path costs **half as many token dollars** — the Batches API discounts every
token including cache reads and writes — and nobody is waiting on a backfill.
The per-search fee is not discounted, so the saving is real but bounded.

Submit and collect are separate commands rather than one that waits, because a
batch may take up to 24 hours and a CI runner blocked on a poll loop bills for
every minute of it. The batch id is the handle; results keep for 29 days.

Both paths build their request with the same `research.request_kwargs`, so what
a batch runs is what `--limit 5` was checked with.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from contextlib import contextmanager
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
    StudioSignal,
    SupportSignal,
    WindowKey,
)
from app.research import (  # noqa: E402
    ResearchError,
    ResearchTarget,
    collect_batch,
    compare_to_curated,
    draft_signals,
    submit_batch,
    summarise_comparisons,
)
from app.rubric import RubricInput, classify  # noqa: E402

logger = logging.getLogger("draft_studio_signals")

DEFAULT_OUT = REPO_ROOT / "data" / "signal_drafts.csv"
VALIDATION_OUT = REPO_ROOT / "data" / "signal_validation.csv"

# Small on purpose: every synchronous row is a call someone is waiting on.
SYNC_DEFAULT_LIMIT = 5

# A validation set is the labelled corner of a 206-row corpus. Anything
# approaching the whole thing means the selection is wrong, and each row is
# a paid call — so refuse rather than discover it on the invoice.
VALIDATION_SANITY_CAP = 60

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
    "alternative_reading",
    "reviewer_note",
]

VALIDATION_FIELDS = [
    "steam_appid",
    "game_name",
    "verdict",
    "curated_studio",
    "drafted_studio",
    "curated_support",
    "drafted_support",
    "confidence",
    "alternative_reading",
    "sources",
    "studio_evidence",
    "reviewer_note",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        help=f"Research at most this many. Defaults to {SYNC_DEFAULT_LIMIT} for the "
        "synchronous path, where each row is a call you wait for, and to the "
        "whole queue for --batch, which is the case batching exists for.",
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
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Queue the run through the Batches API at half the token cost and "
        "exit with the batch id. Nothing is written until --collect.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Research rows whose signals are already curated and report where "
        "the drafts disagree. The queue cannot test this: every documented "
        "closure in the corpus is already labelled, so no unlabelled row can "
        "check whether `closed` is ever called when it should be.",
    )
    parser.add_argument(
        "--collect",
        metavar="BATCH_ID",
        help="Write the review file from a finished batch. Reads the queue again "
        "to recover each row's appid and name, so run it against the same corpus.",
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


def as_row(appid: int, game_name: str, draft) -> dict[str, object]:
    return {
        "steam_appid": appid,
        "game_name": game_name,
        "needs_attention": draft.review_flags,
        "alternative_reading": draft.alternative_reading,
        "studio_signal": draft.studio_signal,
        "support_signal": draft.support_signal,
        "confidence": draft.confidence,
        "sources": " | ".join(draft.sources),
        "studio_evidence": draft.studio_evidence,
        "support_evidence": draft.support_evidence,
        "reviewer_note": draft.reviewer_note,
    }


@contextmanager
def incremental_csv(path: Path, fields: list[str]):
    """Write rows as they arrive, flushed, so an interrupted run keeps them.

    Each row here is a paid call. Writing only after the loop means a job that
    is killed -- the 90-minute ceiling is reachable at 35 rows, and a rate limit
    or a network fault would do the same -- discards everything it gathered and
    bills the re-run from zero. This is the lesson the launch-price job already
    learned, applied late.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        handle.flush()

        def emit(row: dict[str, object]) -> None:
            writer.writerow(row)
            handle.flush()

        yield emit


def write_drafts(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def report(rows: list[dict[str, object]], failed: int, out: Path) -> None:
    print()
    print(f"  drafted            {len(rows)}")
    print(f"  failed             {failed}")
    print(f"  need attention     {sum(1 for r in rows if r['needs_attention'])}")
    print(f"  written to         {out}")
    print()
    print("  These are drafts, not labels. Open the sources, then copy verified")
    print("  values into data/historical_releases.csv by hand.")


def already_answered(session) -> list[HistoricalRelease]:
    """Rows whose two signals are already curated, to measure the drafts against.

    Not `candidates()`: that returns unlabelled rows, and the answers live in
    the labelled ones. Deliberately not filtered to day_one_steam either — the
    researcher's job does not depend on how a game reached Steam, and the
    corpus has only three `closed` rows to spare.

    **`unknown` is not an answer, and excluding it is the whole query.** Both
    columns are `nullable=False, default=UNKNOWN` (app/models.py), so an
    is-not-null test matches all 206 rows rather than the 35 that carry a value.
    The first live run queued every one of them, spent 90 minutes researching 66
    against a curated side that read `unknown`, and was killed having written
    nothing. Every comparison it did make was meaningless.

    The 35 came from counting the CSV, where an uncurated signal is an empty
    string. The job reads the database, where it is UNKNOWN. Checking the number
    against the wrong source is what let the query look right.
    """
    return list(
        session.scalars(
            select(HistoricalRelease)
            .where(HistoricalRelease.studio_signal != StudioSignal.UNKNOWN)
            .where(HistoricalRelease.support_signal != SupportSignal.UNKNOWN)
            .order_by(HistoricalRelease.steam_release_date)
        )
    )


def as_validation_row(row) -> dict[str, object]:
    return {
        "steam_appid": row.key,
        "game_name": row.game_name,
        "verdict": row.verdict,
        "curated_studio": row.curated_studio,
        "drafted_studio": row.draft.studio_signal,
        "curated_support": row.curated_support,
        "drafted_support": row.draft.support_signal,
        "confidence": row.draft.confidence,
        "alternative_reading": row.draft.alternative_reading,
        "sources": " | ".join(row.draft.sources),
        "studio_evidence": row.draft.studio_evidence,
        "reviewer_note": row.draft.reviewer_note,
    }


def report_validation(rows, out: Path, failed: int) -> None:
    tally = summarise_comparisons(rows)
    print()
    print(f"  compared           {tally['compared']}")
    print(f"  failed             {failed}")
    print(f"  studio agrees      {tally['studio_agrees']}/{tally['compared']}")
    print(f"  support agrees     {tally['support_agrees']}/{tally['compared']}")
    print(f"  both agree         {tally['both_agree']}/{tally['compared']}")
    print()
    print("  Direction of the studio misses — these do not average together:")
    print(f"    false benign     {tally['false_benign']}   <- called a gutted studio fine;")
    print("                           buries a Flop as an Underperform")
    print(f"    false alarm      {tally['false_alarm']}   <- called a healthy studio gutted;")
    print("                           the failure the prompt was written against")
    print(f"    refused          {tally['refused']}   <- answered `unknown`; honest, but")
    print("                           many means the window is starving the search")
    print(f"    differs          {tally['differs']}")
    print()
    print(f"  named an alternative  {tally['flagged_alternative']}/{tally['compared']}")
    print(f"  written to         {out}")
    print()
    print("  Measurement, not a target. Fix a named error in a named row; do not")
    print("  tune the prompt against this number. A disagreement may also be the")
    print("  label rather than the draft.")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    with session_scope() as session:
        queue = already_answered(session) if args.validate else candidates(session)
        if args.appids:
            queue = [r for r in queue if r.steam_appid in args.appids]
        if args.validate:
            logger.info("%d release(s) with curated signals to measure against", len(queue))
            if len(queue) > VALIDATION_SANITY_CAP:
                # The run this guard exists for queued 206 rows where 35 were
                # meant, and only the bill said so. A validation set is the
                # labelled corner of the corpus; if it is most of the corpus,
                # the query is wrong and every row is money spent on a
                # comparison against `unknown`.
                logger.error(
                    "%d rows is not a validation set — expected at most %d. The "
                    "signal columns default to UNKNOWN rather than NULL, so a "
                    "null test selects everything. Nothing was researched.",
                    len(queue),
                    VALIDATION_SANITY_CAP,
                )
                return 1
        else:
            logger.info("%d release(s) the rubric cannot resolve without signals", len(queue))

        if args.list_only:
            for release in queue:
                print(f"  {release.steam_appid:<10}{release.game_name}")
            return 0

        # --collect maps results back by appid, so it needs the whole queue as
        # its key space: a batch submitted before someone labelled a row would
        # otherwise return a key this run cannot name.
        if args.collect or args.validate:
            selected = queue
        elif args.batch:
            selected = queue[: args.limit] if args.limit else queue
        elif args.appids:
            # Naming appids is the selection. Silently trimming six named rows
            # to five because a default exists would be its own bug.
            selected = queue[: args.limit] if args.limit else queue
        else:
            selected = queue[: args.limit or SYNC_DEFAULT_LIMIT]

        curated = {
            r.steam_appid: (
                r.studio_signal.value if r.studio_signal else "",
                r.support_signal.value if r.support_signal else "",
            )
            for r in selected
        }
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
            for r in selected
        ]

    if not targets:
        logger.info("Nothing to research.")
        return 0

    import anthropic  # noqa: PLC0415 — only needed on the path that calls the API

    client = anthropic.Anthropic()
    by_key = {str(appid): (appid, target) for appid, target in targets}

    if args.collect and args.validate:
        outcomes = collect_batch(
            client.beta.messages.batches,
            args.collect,
            {key: target.game_name for key, (_, target) in by_key.items()},
        )
        out = args.out if args.out != DEFAULT_OUT else VALIDATION_OUT
        compared, failed = [], 0
        with incremental_csv(out, VALIDATION_FIELDS) as emit:
            for outcome in outcomes:
                known = by_key.get(outcome.key)
                if outcome.draft is None or known is None:
                    failed += 1
                    logger.warning("%s", outcome.error or f"appid {outcome.key} left the set")
                    continue
                appid, target = known
                curated_studio, curated_support = curated[appid]
                row = compare_to_curated(
                    str(appid), target.game_name, curated_studio, curated_support, outcome.draft
                )
                compared.append(row)
                emit(as_validation_row(row))
                logger.info(
                    "%-38s %-13s studio %s/%s  support %s/%s",
                    target.game_name[:38],
                    row.verdict.upper(),
                    outcome.draft.studio_signal,
                    curated_studio,
                    outcome.draft.support_signal,
                    curated_support,
                )
        report_validation(compared, out, failed)
        return 0

    if args.collect:
        outcomes = collect_batch(
            client.beta.messages.batches,
            args.collect,
            {key: target.game_name for key, (_, target) in by_key.items()},
        )
        rows, failed = [], 0
        for outcome in outcomes:
            if outcome.draft is None:
                failed += 1
                logger.warning("%s", outcome.error)
                continue
            known = by_key.get(outcome.key)
            if known is None:
                # The row left the queue between submit and collect — most
                # likely it got labelled. The draft is now moot; say so rather
                # than dropping it silently or crashing on the lookup.
                failed += 1
                logger.warning("appid %s is no longer in the queue; draft ignored", outcome.key)
                continue
            appid, target = known
            rows.append(as_row(appid, target.game_name, outcome.draft))
            logger.info(
                "%-40s studio=%-15s support=%-10s %s",
                target.game_name[:40],
                outcome.draft.studio_signal,
                outcome.draft.support_signal,
                outcome.draft.review_flags,
            )
        rows.sort(key=lambda row: row["steam_appid"])
        write_drafts(args.out, rows)
        report(rows, failed, args.out)
        return 0

    if args.batch:
        batch_id = submit_batch(client.beta.messages.batches, list(by_key.items()))
        kind = "validation row" if args.validate else "row"
        logger.info("queued %d %s(s) at half the token cost", len(by_key), kind)
        collect_flags = f"--collect {batch_id}" + (" --validate" if args.validate else "")
        print()
        print(f"  batch id     {batch_id}")
        print()
        print("  Nothing is written yet, and no runner is waiting — which is the")
        print("  point: the synchronous path spent 90 minutes on 35 rows and was")
        print("  killed by the job ceiling with nothing to show. A batch has no")
        print("  such ceiling. Most finish within the hour; the cap is 24 hours")
        print("  and results keep for 29 days. When it has ended:")
        print()
        print(f"    python jobs/draft_studio_signals.py {collect_flags}")
        print()
        return 0

    if args.validate:
        # Reached only without --batch/--collect: a small, named subset worth
        # waiting on. The whole labelled set belongs in a batch.
        out = args.out if args.out != DEFAULT_OUT else VALIDATION_OUT
        compared, failed = [], 0
        with incremental_csv(out, VALIDATION_FIELDS) as emit:
            for appid, target in targets:
                try:
                    draft = draft_signals(client.beta.messages, target)
                except ResearchError as exc:
                    failed += 1
                    logger.warning("%s", exc)
                    continue
                curated_studio, curated_support = curated[appid]
                row = compare_to_curated(
                    str(appid), target.game_name, curated_studio, curated_support, draft
                )
                compared.append(row)
                emit(as_validation_row(row))
                logger.info(
                    "%-38s %-13s studio %s/%s  support %s/%s",
                    target.game_name[:38],
                    row.verdict.upper(),
                    draft.studio_signal,
                    curated_studio,
                    draft.support_signal,
                    curated_support,
                )
        report_validation(compared, out, failed)
        return 0

    rows, failed = [], 0
    with incremental_csv(args.out, FIELDS) as emit:
        for appid, target in targets:
            try:
                draft = draft_signals(client.beta.messages, target)
            except ResearchError as exc:
                failed += 1
                logger.warning("%s", exc)
                continue
            row = as_row(appid, target.game_name, draft)
            rows.append(row)
            emit(row)
            logger.info(
                "%-40s studio=%-15s support=%-10s %s",
                target.game_name[:40],
                draft.studio_signal,
                draft.support_signal,
                draft.review_flags,
            )

    report(rows, failed, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
