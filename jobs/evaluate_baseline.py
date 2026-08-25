#!/usr/bin/env python3
"""Measure the pre-launch baseline against the labeled set, honestly.

    DATABASE_URL=... python jobs/evaluate_baseline.py

`validate_rubric.py` scores the *post-launch* rubric, which sees how a game
actually launched. This scores the *pre-launch* baseline, which sees only
structural facts known before release — a very different and much harder
problem, and one worth measuring separately so the two are never conflated.

Every forecast excludes the game being forecast from its own evidence, so a
title never informs its own prediction.

The number that matters is not the baseline's accuracy on its own but its
accuracy against always guessing the most common outcome. A model that
cannot beat a constant has not learned anything, and that is the bar Phase 2
has to clear — not the baseline.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.baseline import TIERS  # noqa: E402
from app.baseline import forecast as baseline_forecast  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import HistoricalRelease, PlatformLaunchType  # noqa: E402

logger = logging.getLogger("evaluate_baseline")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Show every prediction.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    with session_scope() as session:
        rows = [
            r
            for r in session.scalars(
                select(HistoricalRelease).where(HistoricalRelease.resolved_outcome.is_not(None))
            )
            if r.platform_launch_type is PlatformLaunchType.DAY_ONE_STEAM
        ]
        if not rows:
            logger.error("no labeled day-one releases to score")
            return 1

        hits = 0
        for release in rows:
            result = baseline_forecast(
                session,
                developer=release.developer,
                publisher=release.publisher,
                on_windows=release.on_windows,
                on_mac=release.on_mac,
                on_linux=release.on_linux,
                exclude_appid=release.steam_appid,
            )
            correct = result.predicted is release.resolved_outcome
            hits += correct
            if args.verbose:
                print(
                    f"  {'OK ' if correct else '   '} {release.game_name[:36]:<36} "
                    f"label={release.resolved_outcome.value:<12} "
                    f"baseline={result.predicted.value}"
                )

        counts = Counter(r.resolved_outcome for r in rows)
        majority, majority_n = counts.most_common(1)[0]
        n = len(rows)

        print()
        print("Pre-launch baseline — structural features only, leave-one-out")
        print("=" * 66)
        print(f"  scored                        {n} day-one Steam releases")
        print(f"  baseline accuracy             {hits}/{n} = {100 * hits / n:.1f}%")
        print(
            f"  always guess '{majority.value}'    {majority_n}/{n} = {100 * majority_n / n:.1f}%"
        )
        print()
        for tier in TIERS:
            print(f"    {tier.value:<14} {counts.get(tier, 0):>3} labeled")
        print()

        if hits <= majority_n:
            print("  The baseline does not beat a constant guess.")
            print("  Structural pre-launch features carry little signal at this sample")
            print("  size. Phase 2's bar is the constant, not the baseline.")
        else:
            print(f"  The baseline beats a constant by {100 * (hits - majority_n) / n:.1f} points.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
