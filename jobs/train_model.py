#!/usr/bin/env python3
"""Train the ordinal outcome model — or decline to, and say why.

    DATABASE_URL=... python jobs/train_model.py

Runs biweekly. Two things can stop it early, and both are normal outcomes
rather than failures:

**No new labels.** Training on an unchanged corpus reproduces the model
already on disk. The run compares a digest of (appid, outcome) pairs against
the last run's and skips when it matches, per the spec's Retraining Cadence.
`--force` overrides it, which is what you want after changing the feature set
or the model itself, since neither moves the label digest.

**The model does not beat a constant guess.** Then no artifact is written and
`/games/{appid}/prediction` keeps serving the rule-based baseline, tagged as
such. This is the same posture as `refresh_company_tiers.py`: measure the
thing, and refuse to ship it when the measurement says it does not work. A
model that loses to "always guess Underperform" and is served anyway is worse
than no model, because the dashboard would attach a confidence number to it.

The bar is deliberately the constant and not the rule-based baseline. The
baseline itself does not clear the constant, so beating it would prove
nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

from app.db import session_scope  # noqa: E402
from app.features import FEATURE_NAMES, build_rows  # noqa: E402
from app.ordinal import DEFAULT_ARTIFACT, TIERS, save  # noqa: E402
from ml.train import (  # noqa: E402
    Evaluation,
    NotEnoughLabels,
    beats_constant,
    build_artifact,
    evaluate,
    fingerprint,
    last_run,
    record_run,
)

logger = logging.getLogger("train_model")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retrain even when the labels are unchanged since the last run.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Evaluate and print, but never write an artifact.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show the per-game held-out result.",
    )
    return parser.parse_args(argv)


def print_report(evaluation: Evaluation, *, verbose: bool) -> None:
    print()
    print("Ordinal outcome model — repeated stratified k-fold, every row held out")
    print("=" * 72)
    print(f"  corpus                        {evaluation.n_rows} labeled day-one releases")
    print(
        f"  protocol                      {evaluation.n_splits}-fold x "
        f"{evaluation.n_repeats} repeats"
    )
    print(f"  features                      {len(FEATURE_NAMES)} pre-launch")
    print()
    for tier in TIERS:
        print(f"    {tier.value:<14} {evaluation.tier_counts.get(tier, 0):>3} labeled")
    print()
    print(f"  {'':<30}{'model':>10}{'constant':>12}")
    print(
        f"  {'accuracy':<30}{100 * evaluation.model_accuracy:>9.1f}%"
        f"{100 * evaluation.constant_accuracy:>11.1f}%"
    )
    print(
        f"  {'mean ordinal distance':<30}{evaluation.model_distance:>10.2f}"
        f"{evaluation.constant_distance:>12.2f}"
    )
    print()
    print(
        f"  distance improvement          {evaluation.improvement:+.2f} tiers "
        f"(95% CI low {evaluation.improvement_ci_low:+.2f})"
    )
    print(f"  verdict                       {evaluation.verdict}")
    print()

    if verbose:
        print("  Per game, averaged over every repeat that held it out:")
        for row in sorted(evaluation.rows, key=lambda r: -r.model_distance):
            print(
                f"    {row.game_name[:34]:<34} actual={row.actual.value:<12} "
                f"usually={row.modal_prediction.value:<12} "
                f"hit={100 * row.model_accuracy:>5.1f}%  dist={row.model_distance:.2f}"
            )
        print()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    with session_scope() as session:
        rows = build_rows(session)

    if not rows:
        logger.error("no labeled day-one Steam releases to train on")
        return 1

    digest = fingerprint(rows)
    previous = last_run()
    if not args.force and previous and previous.get("label_fingerprint") == digest:
        logger.info(
            "Labels unchanged since %s (%s) — skipping. Use --force after a "
            "feature or model change, which the digest does not track.",
            previous.get("ran_at", "the last run"),
            digest,
        )
        return 0

    try:
        evaluation = evaluate(rows)
    except NotEnoughLabels as exc:
        logger.error("%s", exc)
        logger.error(
            "The corpus needs more resolved outcomes before a model can be "
            "measured at all. The baseline keeps serving until then."
        )
        return 1

    print_report(evaluation, verbose=args.verbose)

    if args.report_only:
        logger.info("--report-only: no artifact written, no run recorded.")
        return 0

    passed = beats_constant(evaluation)
    if passed:
        path = save(build_artifact(rows, evaluation))
        logger.info("Model beats the constant. Wrote %s", path)
    else:
        # Leaving a stale artifact behind would keep serving a model this run
        # just established is no better than a constant.
        if DEFAULT_ARTIFACT.exists():
            DEFAULT_ARTIFACT.unlink()
            logger.warning("Removed the previous artifact: it no longer clears the bar.")
        logger.warning(
            "Model does not beat a constant guess — no artifact written. "
            "/games/{appid}/prediction keeps serving the rule-based baseline."
        )

    record_run(evaluation, digest, artifact_written=passed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
