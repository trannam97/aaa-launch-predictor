"""Derive a categorical budget tier per publisher, by unsupervised clustering.

Budget figures aren't public, so the spec's approach is to cluster companies
on observable behaviour instead of estimating dollars. This runs quarterly,
writes a `company_tiers` lookup table, and the prediction model joins against
it — clusters are never computed per prediction.

**Three of the spec's six intended features are unavailable** and their
absence is worth stating plainly rather than hiding in a mean:
headcount (needs MobyGames credits or studio pages), confirmed upcoming
titles (the corpus is historical), and revenue/market-cap bracket. What
remains is catalog size, platform reach, average review score, and release
scale. The tiers are therefore a shape derived from release behaviour, not a
budget estimate, and the spec's instruction to hand-review them before
trusting them matters more, not less.

**Result as of Aug 2026: the clustering does not produce usable tiers, and
the job refuses to write them.** Measured on 26 publishers, k-means is
unstable (mean adjusted Rand 0.33 across seeds — Activision lands with
Ubisoft in 5 runs of 12, Blizzard in 3) and silhouette peaks at 0.26, below
the level at which cluster structure is considered real. Worse, the split is
driven the wrong way: tier correlates -0.67 with mean review score and -0.60
with mean launch volume, so the "major" tier is simply the prolific-but-
middling publishers. That is a performance profile, not a budget tier, and
using it to predict performance would be circular.

`stability()` measures this on every run and `refresh_company_tiers.py`
writes tiers only when it passes. Publisher aggregates are written
regardless — they are useful on their own, and a gradient-boosted tree can
use them as continuous features without the information loss that bucketing
into three tiers imposes.

**Known leakage.** `mean_volume_percentile` summarises how big a company's
launches were, so a company's tier partly reflects games that may also be
training rows. For forecasting a genuinely new title that is legitimate — a
publisher's past record is known before their next game ships. For
*evaluating* on historical rows it is not: Phase 2 must exclude a game from
its own company's aggregate, or the tier will quietly encode the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cohort import CohortIndex
from app.companies import normalize_all
from app.models import HistoricalRelease, ReleaseWindow, WindowKey

# A company needs at least this many titles before a mean over them says
# anything. Below it the tier is left unassigned rather than guessed.
MIN_TITLES = 2

# Clustering is deterministic so a quarterly re-run doesn't reshuffle tiers
# for reasons nobody can trace.
RANDOM_STATE = 20260824


@dataclass(slots=True)
class CompanyFeatures:
    name: str
    title_count: int
    mean_volume_percentile: float
    mean_positive_pct: float
    mean_platform_breadth: float
    active_span_years: int
    first_year: int | None = None
    last_year: int | None = None

    def vector(self) -> list[float]:
        # Catalog size is logged: the gap between 1 and 5 titles says far
        # more about a publisher than the gap between 30 and 34.
        return [
            float(np.log1p(self.title_count)),
            self.mean_volume_percentile,
            self.mean_positive_pct,
            self.mean_platform_breadth,
            float(self.active_span_years),
        ]


@dataclass(slots=True)
class TierAssignment:
    company: CompanyFeatures
    tier: int  # 1 = smallest scale, ascending
    tier_label: str


TIER_LABELS_BY_K = {
    2: ["mid", "major"],
    3: ["small", "mid", "major"],
    4: ["small", "mid", "large", "major"],
}


def extract_features(session: Session) -> list[CompanyFeatures]:
    """Aggregate the corpus into one row per normalized publisher."""
    index = CohortIndex.from_db(session)
    windows = {
        w.release_id: w
        for w in session.scalars(
            select(ReleaseWindow).where(ReleaseWindow.window_key == WindowKey.LAUNCH_2W)
        )
    }

    buckets: dict[str, list[HistoricalRelease]] = {}
    for release in session.scalars(select(HistoricalRelease)):
        for company in normalize_all(release.publisher):
            buckets.setdefault(company, []).append(release)

    features: list[CompanyFeatures] = []
    for name, releases in buckets.items():
        if len(releases) < MIN_TITLES:
            continue

        percentiles, sentiments, breadths, years = [], [], [], []
        for release in releases:
            window = windows.get(release.id)
            if window is not None and window.review_total is not None:
                percentile, _ = index.percentile(release.cohort_year, window.review_total)
                if percentile is not None:
                    percentiles.append(percentile)
                if window.positive_pct is not None:
                    sentiments.append(window.positive_pct)
            breadths.append(sum((release.on_windows, release.on_mac, release.on_linux)))
            if release.cohort_year:
                years.append(release.cohort_year)

        if not percentiles or not sentiments:
            continue

        features.append(
            CompanyFeatures(
                name=name,
                title_count=len(releases),
                mean_volume_percentile=round(float(np.mean(percentiles)), 2),
                mean_positive_pct=round(float(np.mean(sentiments)), 2),
                mean_platform_breadth=round(float(np.mean(breadths)), 2),
                active_span_years=(max(years) - min(years)) if years else 0,
                first_year=min(years) if years else None,
                last_year=max(years) if years else None,
            )
        )

    return sorted(features, key=lambda f: f.name)


def _matrix(features: list[CompanyFeatures]) -> np.ndarray:
    return StandardScaler().fit_transform(np.array([f.vector() for f in features]))


def silhouettes(features: list[CompanyFeatures], ks: tuple[int, ...] = (2, 3, 4, 5)) -> dict:
    """Silhouette score per k, to choose a cluster count with evidence."""
    if len(features) < max(ks) + 1:
        return {}
    matrix = _matrix(features)
    scores = {}
    for k in ks:
        labels = KMeans(n_clusters=k, n_init=25, random_state=RANDOM_STATE).fit_predict(matrix)
        scores[k] = round(float(silhouette_score(matrix, labels)), 3)
    return scores


def cluster(features: list[CompanyFeatures], k: int = 3) -> list[TierAssignment]:
    """Cluster companies and return tiers ordered smallest-scale first.

    k-means labels are arbitrary, so they are re-mapped by each cluster's
    mean scale. Without that, "tier 2" would mean something different on
    every quarterly run.
    """
    if len(features) < k:
        raise ValueError(f"need at least {k} companies to form {k} clusters")

    matrix = _matrix(features)
    labels = KMeans(n_clusters=k, n_init=25, random_state=RANDOM_STATE).fit_predict(matrix)

    # Rank clusters by scale: catalog size and typical launch size together.
    scale: dict[int, float] = {}
    for label in set(labels):
        members = [f for f, m in zip(features, labels, strict=True) if m == label]
        scale[label] = float(
            np.mean([np.log1p(f.title_count) for f in members])
            + np.mean([f.mean_volume_percentile / 100 for f in members])
        )
    order = {label: rank for rank, label in enumerate(sorted(scale, key=lambda x: scale[x]), 1)}
    names = TIER_LABELS_BY_K.get(k, [str(i) for i in range(1, k + 1)])

    return [
        TierAssignment(company=f, tier=order[label], tier_label=names[order[label] - 1])
        for f, label in zip(features, labels, strict=True)
    ]


# Cluster structure has to clear both bars before tiers are trusted. Below
# them, k-means still returns labels — it always does — and they mean nothing.
MIN_SILHOUETTE = 0.35
MIN_STABILITY = 0.75


@dataclass(slots=True)
class Stability:
    """Whether the clustering is reproducible enough to act on."""

    k: int
    silhouette: float
    adjusted_rand: float

    @property
    def is_trustworthy(self) -> bool:
        return self.silhouette >= MIN_SILHOUETTE and self.adjusted_rand >= MIN_STABILITY

    @property
    def verdict(self) -> str:
        if self.is_trustworthy:
            return "stable"
        problems = []
        if self.silhouette < MIN_SILHOUETTE:
            problems.append(f"silhouette {self.silhouette:.2f} < {MIN_SILHOUETTE}")
        if self.adjusted_rand < MIN_STABILITY:
            problems.append(f"seed agreement {self.adjusted_rand:.2f} < {MIN_STABILITY}")
        return "; ".join(problems)


def stability(features: list[CompanyFeatures], k: int = 3, seeds: int = 12) -> Stability:
    """Re-cluster under different seeds and measure how much the answer moves.

    k-means always returns a partition, so the question is never "did it
    cluster" but "would it have clustered the same way twice". Partitions
    that disagree between seeds describe the seed, not the companies.
    """
    matrix = _matrix(features)
    runs = [
        KMeans(n_clusters=k, n_init=1, random_state=seed).fit_predict(matrix)
        for seed in range(seeds)
    ]
    pairwise = [
        adjusted_rand_score(runs[i], runs[j])
        for i in range(len(runs))
        for j in range(i + 1, len(runs))
    ]
    reference = KMeans(n_clusters=k, n_init=25, random_state=RANDOM_STATE).fit_predict(matrix)
    return Stability(
        k=k,
        silhouette=round(float(silhouette_score(matrix, reference)), 3),
        adjusted_rand=round(float(np.mean(pairwise)), 3),
    )
