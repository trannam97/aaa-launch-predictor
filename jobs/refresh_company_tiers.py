#!/usr/bin/env python3
"""Quarterly: refresh publisher aggregates, and cluster them if they hold up.

    DATABASE_URL=... python jobs/refresh_company_tiers.py
    DATABASE_URL=... python jobs/refresh_company_tiers.py --k 4 --dry-run

Two separable things happen here, and only one of them currently succeeds.

Publisher aggregates are always written — catalog size, typical launch
volume and sentiment, platform breadth, active span. They are useful on
their own and carry more information than any bucketing of them would.

Tiers are written **only if the clustering passes a stability check**. The
spec's instruction was to hand-review clusters before trusting them; this
automates the part of that review a machine can do, so a future re-run picks
up real structure if the corpus ever develops it, and stays silent until
then. The cluster table is printed either way, for the human half.

Requires scikit-learn (see ml/requirements.txt); the backend does not.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import PublisherStats, utcnow  # noqa: E402
from ml.company_tiering import (  # noqa: E402
    MIN_TITLES,
    cluster,
    extract_features,
    silhouettes,
    stability,
)

logger = logging.getLogger("refresh_company_tiers")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=3, help="Cluster count (default: %(default)s).")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument(
        "--force-tiers",
        action="store_true",
        help="Write tiers even if the stability check fails. For experiments only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    with session_scope() as session:
        features = extract_features(session)
        if not features:
            logger.error("no publishers with at least %d titles; nothing to do", MIN_TITLES)
            return 1

        logger.info("%d publishers with >= %d titles", len(features), MIN_TITLES)
        logger.info("silhouette by k: %s", silhouettes(features))

        check = stability(features, k=args.k)
        trusted = check.is_trustworthy or args.force_tiers
        logger.info(
            "k=%d stability: silhouette=%.3f seed-agreement=%.3f -> %s",
            check.k,
            check.silhouette,
            check.adjusted_rand,
            check.verdict,
        )

        assignments = cluster(features, args.k)
        _print_review_table(assignments, trusted)

        if not trusted:
            logger.warning(
                "clusters are not stable enough to trust; writing aggregates only, "
                "leaving tier NULL. Re-run as the corpus grows."
            )

        if args.dry_run:
            logger.info("dry run; nothing written")
            session.rollback()
            return 0

        written = _write(session, assignments, trusted)
        logger.info("wrote %d publisher rows (tier populated: %s)", written, trusted)

    return 0


def _print_review_table(assignments, trusted: bool) -> None:
    """The human half of the hand-review the spec asks for."""
    header = "tier" if trusted else "tier*"
    print()
    print(
        f"  {header:<6} {'publisher':<30} {'n':>3} {'vol%':>6} {'pos%':>6} {'plat':>5} {'span':>5}"
    )
    print("  " + "-" * 68)
    for a in sorted(assignments, key=lambda a: (-a.tier, -a.company.title_count)):
        c = a.company
        label = f"{a.tier} {a.tier_label}" if trusted else f"({a.tier})"
        print(
            f"  {label:<6} {c.name[:30]:<30} {c.title_count:>3} "
            f"{c.mean_volume_percentile:>6.1f} {c.mean_positive_pct:>6.1f} "
            f"{c.mean_platform_breadth:>5.1f} {c.active_span_years:>5}"
        )
    if not trusted:
        print("\n  * shown for review only — not written, clustering failed its stability check")
    print()


def _write(session, assignments, trusted: bool) -> int:
    existing = {row.name: row for row in session.scalars(select(PublisherStats))}
    for a in assignments:
        c = a.company
        row = existing.get(c.name)
        if row is None:
            row = PublisherStats(name=c.name)
            session.add(row)
        row.title_count = c.title_count
        row.mean_volume_percentile = c.mean_volume_percentile
        row.mean_positive_pct = c.mean_positive_pct
        row.mean_platform_breadth = c.mean_platform_breadth
        row.first_year = c.first_year
        row.last_year = c.last_year
        row.tier = a.tier if trusted else None
        row.tier_label = a.tier_label if trusted else None
        row.computed_at = utcnow()
    return len(assignments)


if __name__ == "__main__":
    raise SystemExit(main())
