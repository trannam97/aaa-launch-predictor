#!/usr/bin/env python3
"""Score the rubric against the hand-labeled set and print the result.

Phase 1's checkpoint: run this after changing a threshold in app/rubric.py or
adding labels, and read the disagreements — they are where the rubric is
still wrong.

    DATABASE_URL=... python jobs/validate_rubric.py
    DATABASE_URL=... python jobs/validate_rubric.py --json

Exits non-zero if agreement on the falsifiable axis falls below --min-agreement,
so it can gate CI once the labeled set is large enough to be worth gating on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db import session_scope  # noqa: E402
from app.models import Outcome  # noqa: E402
from app.validation import validate  # noqa: E402

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

        if args.json:
            print(json.dumps(_as_dict(report), indent=2))
        else:
            _print_report(report)

        agreement = report.met_expectations_agreement

    if agreement < args.min_agreement:
        print(f"\nFAIL: met-expectations agreement {agreement}% < {args.min_agreement}%")
        return 1
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
