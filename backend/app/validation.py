"""Score the rubric against the hand-labeled set.

Phase 1 exists to check the rubric before any model is trained, so this
reports the numbers that would actually change the rubric — not a single
accuracy figure that hides where it goes wrong.

Only day-one Steam releases are scored. For a delayed port the label
describes a launch that happened on another platform months or years earlier,
while the features describe the Steam window, so scoring it would measure
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cohort import CohortIndex
from app.models import (
    HistoricalRelease,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    WindowKey,
)
from app.rubric import RubricInput, RubricResult, classify

# Labels for these launch types describe a different event than the Steam
# window the features are drawn from.
ELIGIBLE_LAUNCH_TYPES = {PlatformLaunchType.DAY_ONE_STEAM}


@dataclass(slots=True)
class Scored:
    release: HistoricalRelease
    expected: Outcome
    result: RubricResult
    volume_percentile: float | None
    positive_pct: float | None
    retention_ratio: float | None = None

    @property
    def predicted(self) -> Outcome | None:
        return self.result.outcome

    @property
    def agrees(self) -> bool:
        return self.predicted is self.expected

    @property
    def tier_distance(self) -> int | None:
        if self.predicted is None:
            return None
        return abs(self.predicted.rank - self.expected.rank)


@dataclass(slots=True)
class ValidationReport:
    scored: list[Scored] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)

    @property
    def resolved(self) -> list[Scored]:
        return [s for s in self.scored if s.predicted is not None]

    @property
    def unresolved(self) -> list[Scored]:
        return [s for s in self.scored if s.predicted is None]

    @property
    def exact_agreement(self) -> float:
        if not self.resolved:
            return 0.0
        return round(100.0 * sum(s.agrees for s in self.resolved) / len(self.resolved), 1)

    @property
    def met_expectations_agreement(self) -> float:
        """Agreement on the falsifiable axis: did it meet expectations at all?

        This is the number worth trusting. It collapses the four tiers into
        the below/at-or-above split that cohort-normalized volume decides on
        its own, with no input from the studio-outcome evidence that also
        informed the hand label.
        """
        pairs = [
            (s.predicted.rank >= Outcome.SUCCESS.rank, s.expected.rank >= Outcome.SUCCESS.rank)
            for s in self.resolved
        ]
        if not pairs:
            return 0.0
        return round(100.0 * sum(a == b for a, b in pairs) / len(pairs), 1)

    @property
    def mean_tier_distance(self) -> float:
        """Average ordinal miss. A Flop/Breakout confusion costs 3, not 1."""
        distances = [s.tier_distance for s in self.resolved]
        if not distances:
            return 0.0
        return round(sum(distances) / len(distances), 2)

    @property
    def confusion(self) -> dict[tuple[Outcome, Outcome], int]:
        table: dict[tuple[Outcome, Outcome], int] = {}
        for s in self.resolved:
            key = (s.expected, s.predicted)
            table[key] = table.get(key, 0) + 1
        return table

    @property
    def disagreements(self) -> list[Scored]:
        return sorted(
            (s for s in self.resolved if not s.agrees),
            key=lambda s: -(s.tier_distance or 0),
        )


def validate(session: Session) -> ValidationReport:
    """Run every eligible labeled release through the rubric."""
    index = CohortIndex.from_db(session)
    report = ValidationReport()

    releases = session.scalars(
        select(HistoricalRelease)
        .where(HistoricalRelease.resolved_outcome.is_not(None))
        .order_by(HistoricalRelease.steam_release_date)
    ).all()

    windows = {
        (w.release_id, w.window_key): w
        for w in session.scalars(
            select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_2W)
        )
    }
    # Three-month counts give the retention ratio the rubric uses to tell a
    # launch spike that held from one that collapsed.
    windows_3m = {
        (w.release_id, w.window_key): w
        for w in session.scalars(
            select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_3M)
        )
    }

    for release in releases:
        if release.platform_launch_type not in ELIGIBLE_LAUNCH_TYPES:
            report.excluded.append(
                (release.game_name, f"launch type is {release.platform_launch_type.value}")
            )
            continue

        window = windows.get((release.id, WindowKey.LAUNCH_2W))
        if window is None or window.review_total is None:
            report.excluded.append((release.game_name, "no launch-window review data"))
            continue

        percentile, stats = index.percentile(release.cohort_year, window.review_total)
        late = windows_3m.get((release.id, WindowKey.LAUNCH_3M))
        retention = (
            late.review_total / window.review_total
            if late and late.review_total and window.review_total
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
        report.scored.append(
            Scored(
                release=release,
                expected=release.resolved_outcome,
                result=result,
                volume_percentile=percentile,
                positive_pct=window.positive_pct,
                retention_ratio=retention,
            )
        )

    return report
