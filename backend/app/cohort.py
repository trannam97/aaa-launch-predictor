"""Cohort normalization for count-based features.

Raw counts are not comparable across years. The Witcher 3 drew 7,519 reviews
in its first two weeks in 2015; Black Myth: Wukong drew 689,276 in 2024. That
is Steam's install base and review-leaving culture growing, not a 90x
difference in success — so every count-based feature is ranked within a
cohort of same-era releases rather than used raw.

Cohorts are a rolling window of release years rather than a single year:
with only a handful of AAA releases per year in the dataset, a strict
same-year cohort gives percentiles too coarse to be meaningful.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HistoricalRelease, ReleaseWindow, WindowKey

# Half-width of the cohort window, in years. A game released in 2020 with
# COHORT_HALF_WIDTH=1 is ranked against 2019-2021 releases.
COHORT_HALF_WIDTH = 1

# Below this many peers a percentile is too noisy to act on, and the caller
# is told so rather than handed a confident-looking number.
MIN_COHORT_SIZE = 8


@dataclass(slots=True)
class CohortStats:
    """The reference distribution a single game is scored against."""

    year: int
    values: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.values)

    @property
    def is_reliable(self) -> bool:
        return self.size >= MIN_COHORT_SIZE

    @property
    def median(self) -> int | None:
        if not self.values:
            return None
        mid = self.size // 2
        if self.size % 2:
            return self.values[mid]
        return (self.values[mid - 1] + self.values[mid]) // 2

    def percentile_of(self, value: int) -> float:
        """Percentile rank of `value` in this cohort, 0-100.

        Ties are split across the tied block (the midpoint of its lower and
        upper bounds), so a game does not get credit for beating peers it
        merely matched.
        """
        if not self.values:
            return 50.0
        below = bisect_left(self.values, value)
        at_or_below = bisect_right(self.values, value)
        return round(100.0 * (below + at_or_below) / (2 * self.size), 1)


class CohortIndex:
    """Launch-window review counts grouped into rolling year cohorts."""

    def __init__(self, counts_by_year: dict[int, list[int]]) -> None:
        self._by_year = {year: sorted(v) for year, v in counts_by_year.items()}

    @classmethod
    def from_db(cls, session: Session, window_key: WindowKey = WindowKey.LAUNCH_2W) -> CohortIndex:
        """Build the index from every backfilled release with a count.

        Unlabeled rows are included on purpose: the cohort describes what a
        normal launch looked like that year, and that does not depend on
        whether anyone has researched the outcome.
        """
        rows = session.execute(
            select(HistoricalRelease.cohort_year, ReleaseWindow.review_total)
            .join(ReleaseWindow, ReleaseWindow.release_id == HistoricalRelease.id)
            .where(
                ReleaseWindow.window_key == window_key,
                ReleaseWindow.review_total.is_not(None),
                HistoricalRelease.cohort_year.is_not(None),
            )
        ).all()

        counts: dict[int, list[int]] = {}
        for year, total in rows:
            counts.setdefault(year, []).append(total)
        return cls(counts)

    def stats_for(self, year: int) -> CohortStats:
        values: list[int] = []
        for offset in range(-COHORT_HALF_WIDTH, COHORT_HALF_WIDTH + 1):
            values.extend(self._by_year.get(year + offset, []))
        return CohortStats(year=year, values=sorted(values))

    def percentile(self, year: int | None, value: int | None) -> tuple[float | None, CohortStats]:
        """Percentile of `value` within `year`'s cohort.

        Returns (None, stats) when the input is missing or the cohort is too
        small to rank against — an unreliable percentile is worse than none,
        because everything downstream would treat it as real.
        """
        stats = self.stats_for(year) if year is not None else CohortStats(year=0)
        if value is None or year is None or not stats.is_reliable:
            return None, stats
        return stats.percentile_of(value), stats

    @property
    def years(self) -> list[int]:
        return sorted(self._by_year)
