#!/usr/bin/env python3
"""Score the rubric against the hand-labeled set and print the result.

Phase 1's checkpoint: run this after changing a threshold in app/rubric.py or
adding labels, and read the disagreements — they are where the rubric is
still wrong.

    DATABASE_URL=... python jobs/validate_rubric.py
    DATABASE_URL=... python jobs/validate_rubric.py --json

Exits non-zero if agreement on the falsifiable axis falls below --min-agreement,
so it can gate CI once the labeled set is large enough to be worth gating on.

**This reads the database, not the corpus CSV.** Labels cross over only when
`backfill` runs, so editing data/historical_releases.csv and coming straight
here reports the previous corpus with nothing on screen to say so. Every run
now compares the two and says so loudly when they differ.
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import pairwise
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.backfill import load_curated_csv  # noqa: E402
from app.cohort import PriceIndex  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease, Outcome  # noqa: E402
from app.validation import validate  # noqa: E402

CURATED_CSV = REPO_ROOT / "data" / "historical_releases.csv"

# The fields that decide a row's tier, and so the ones whose drift makes this
# report describe a corpus that is no longer on disk. Prose fields are left
# out: a reworded note changes nothing the rubric reads.
LABEL_FIELDS = (
    "resolved_outcome",
    "studio_signal",
    "support_signal",
    "label_confidence",
    "research_status",
    "platform_launch_type",
)

TIERS = [Outcome.FLOP, Outcome.UNDERPERFORM, Outcome.SUCCESS, Outcome.BREAKOUT]
SHORT = {
    Outcome.FLOP: "flop",
    Outcome.UNDERPERFORM: "under",
    Outcome.SUCCESS: "succ",
    Outcome.BREAKOUT: "break",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    parser.add_argument(
        "--no-drift-check",
        action="store_true",
        help="Skip the corpus-vs-database comparison (for a deliberately divergent database).",
    )
    parser.add_argument(
        "--min-agreement",
        type=float,
        default=0.0,
        help="Fail if met-expectations agreement falls below this percentage.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with session_scope() as session:
        report = validate(session)
        drift = [] if args.no_drift_check else _curated_drift(session)

        if args.json:
            payload = _as_dict(report)
            payload["curated_drift"] = [
                {"game": g, "field": f, "db": db, "csv": csv_} for g, f, db, csv_ in drift
            ]
            print(json.dumps(payload, indent=2))
        else:
            _print_drift(drift)
            _print_report(report)
            _print_price_context(session)

        agreement = report.met_expectations_agreement

    # A stale database is worse than a low score: the number looks fine and is
    # about the wrong corpus. Never gate CI on one.
    if drift and args.min_agreement > 0:
        print(
            f"\nFAIL: {len(drift)} field(s) differ from {CURATED_CSV.name}; "
            "run backfill before gating on these numbers."
        )
        return 1

    if agreement < args.min_agreement:
        print(f"\nFAIL: met-expectations agreement {agreement}% < {args.min_agreement}%")
        return 1
    return 0


def _curated_drift(session) -> list[tuple[str, str, str, str]]:
    """Label-bearing fields that differ between the corpus CSV and the database.

    Returns (game, field, db_value, csv_value) per difference. Rows absent from
    the database are reported as a whole-row difference: they are exactly the
    rows a missing backfill would leave out of the score.
    """
    try:
        curated = {row.steam_appid: row for row in load_curated_csv(CURATED_CSV)}
    except (OSError, ValueError):
        # A CSV that cannot be read is the backfill job's problem to report,
        # not a reason to withhold the validation numbers.
        return []

    stored = {r.steam_appid: r for r in session.scalars(select(HistoricalRelease))}
    drift: list[tuple[str, str, str, str]] = []
    for appid, row in sorted(curated.items()):
        if row.resolved_outcome is None:
            continue
        release = stored.get(appid)
        if release is None:
            drift.append((f"appid {appid}", "row", "absent", "labeled"))
            continue
        for field in LABEL_FIELDS:
            want, got = getattr(row, field), getattr(release, field)
            if want != got:
                drift.append((release.game_name, field, _show(got), _show(want)))
    return drift


def _show(value) -> str:
    return "unset" if value is None else getattr(value, "value", str(value))


def _print_drift(drift: list[tuple[str, str, str, str]]) -> None:
    if not drift:
        return
    print("!" * 74)
    print(f"!! THE DATABASE IS STALE — {len(drift)} field(s) differ from {CURATED_CSV.name}.")
    print("!! Every number below describes the database, not the corpus on disk.")
    for game, field, db_value, csv_value in drift:
        print(f"!!   {game[:38]:<38} {field:<20} db={db_value:<14} csv={csv_value}")
    print("!! Run backfill, then run this again.")
    print("!" * 74)
    print()


def _as_dict(report) -> dict:
    return {
        "scored": len(report.scored),
        "resolved": len(report.resolved),
        "unresolved": len(report.unresolved),
        "excluded": [{"game": g, "reason": r} for g, r in report.excluded],
        "exact_agreement_pct": report.exact_agreement,
        "met_expectations_agreement_pct": report.met_expectations_agreement,
        "mean_tier_distance": report.mean_tier_distance,
        "disagreements": [
            {
                "game": s.release.game_name,
                "expected": s.expected.value,
                "predicted": s.predicted.value if s.predicted else None,
                "volume_percentile": s.volume_percentile,
                "positive_pct": s.positive_pct,
                "reasons": s.result.reasons,
            }
            for s in report.disagreements
        ],
    }


def _print_report(report) -> None:
    print("Rubric validation — hand labels vs. app/rubric.py")
    print("=" * 74)
    print(f"  scored              {len(report.scored)} day-one Steam releases")
    print(f"  resolved by rubric  {len(report.resolved)}")
    print(f"  left unresolved     {len(report.unresolved)}")
    print(f"  excluded            {len(report.excluded)}")
    print()
    print(
        f"  met-expectations agreement   {report.met_expectations_agreement:>5}%   "
        "<- the falsifiable axis"
    )
    print(f"  exact 4-tier agreement       {report.exact_agreement:>5}%")
    print(f"  mean ordinal distance        {report.mean_tier_distance:>5}")
    print()

    print("  Confusion (rows = hand label, columns = rubric)")
    table = report.confusion
    header = "".join(f"{SHORT[c]:>7}" for c in TIERS)
    print(f"    {'':<8}{header}")
    for expected in TIERS:
        cells = "".join(f"{table.get((expected, pred), 0):>7}" for pred in TIERS)
        print(f"    {SHORT[expected]:<8}{cells}")
    print()

    if report.disagreements:
        print("  Disagreements, worst ordinal miss first")
        for s in report.disagreements:
            pct = f"{s.volume_percentile:.0f}th" if s.volume_percentile is not None else "n/a"
            pos = f"{s.positive_pct:.0f}%" if s.positive_pct is not None else "n/a"
            print(
                f"    {s.release.game_name[:38]:<38} label={s.expected.value:<12} "
                f"rubric={s.predicted.value:<12} vol={pct:<6} pos={pos}"
            )
            for reason in s.result.reasons:
                print(f"        - {reason}")
        print()

    if report.unresolved:
        print("  Unresolved by the rubric (reported, not guessed)")
        for s in report.unresolved:
            print(f"    {s.release.game_name[:38]:<38} {s.result.unresolved_reason}")
        print()

    if report.excluded:
        print("  Excluded from scoring")
        for game, reason in report.excluded:
            print(f"    {game[:38]:<38} {reason}")
        print()


def _print_price_context(session) -> None:
    """Show the going rate per cohort — the reference nominal price is ranked against."""
    prices = PriceIndex.from_db(session)
    years = sorted({y for y in range(2014, 2027) if prices.going_rate(y)})
    print("  Going launch price by cohort (modal price of comparable releases)")
    if not years:
        print("    not enough curated launch prices to establish a rate for any cohort")
        return
    rates = [(year, prices.going_rate(year)) for year in years]
    for year, rate in rates:
        print(f"    {year}   ${rate / 100:.0f}")
    print(f"    {_price_note(rates)}")


def _dollars(cents: int) -> str:
    return f"${cents / 100:.0f}"


def _price_note(rates: list[tuple[int, int]]) -> str:
    """Describe the going-rate table from the table itself.

    This line used to be a constant, and drifted away from the rows printed
    directly above it: it claimed $60 was the going rate "through 2022" while
    the table put the change at 2024. Prose under a computed table has to be
    computed too, or it becomes a confident statement of a stale fact.
    """
    changes = [(year, rate) for (_, prev), (year, rate) in pairwise(rates) if rate != prev]
    if not changes:
        return f"The going rate held at {_dollars(rates[0][1])} across every cohort here."
    spans = [f"{_dollars(rates[0][1])} through {changes[0][0] - 1}"]
    for i, (year, rate) in enumerate(changes):
        last = changes[i + 1][0] - 1 if i + 1 < len(changes) else rates[-1][0]
        spans.append(f"{_dollars(rate)} from {year}" + ("" if year == last else f" to {last}"))
    return (
        "Nominal price is not comparable across these — the going rate was "
        + ", then ".join(spans)
        + "."
    )


if __name__ == "__main__":
    raise SystemExit(main())
