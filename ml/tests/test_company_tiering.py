"""Tests for the company clustering and, more importantly, its refusal to trust itself."""

from __future__ import annotations

import pytest

from ml.company_tiering import (
    MIN_SILHOUETTE,
    MIN_STABILITY,
    CompanyFeatures,
    Stability,
    cluster,
)


def company(name: str, titles: int, vol: float, pos: float) -> CompanyFeatures:
    return CompanyFeatures(
        name=name,
        title_count=titles,
        mean_volume_percentile=vol,
        mean_positive_pct=pos,
        mean_platform_breadth=1.0,
        active_span_years=5,
    )


def test_tiers_are_ordered_by_scale_not_by_kmeans_label():
    # k-means labels are arbitrary. Without re-mapping, "tier 2" would mean
    # something different on every quarterly run.
    small = [company(f"small{i}", 1, 10.0, 60.0) for i in range(5)]
    big = [company(f"big{i}", 40, 95.0, 90.0) for i in range(5)]

    assignments = {a.company.name: a for a in cluster(small + big, k=2)}

    assert all(assignments[f"small{i}"].tier == 1 for i in range(5))
    assert all(assignments[f"big{i}"].tier == 2 for i in range(5))


def test_cluster_refuses_when_there_are_fewer_companies_than_clusters():
    with pytest.raises(ValueError, match="at least 3"):
        cluster([company("a", 2, 50.0, 80.0), company("b", 3, 60.0, 70.0)], k=3)


def test_stability_accepts_only_when_both_bars_are_cleared():
    good = Stability(k=3, silhouette=MIN_SILHOUETTE, adjusted_rand=MIN_STABILITY)

    assert good.is_trustworthy is True
    assert good.verdict == "stable"


@pytest.mark.parametrize(
    ("silhouette", "rand", "expected_in_verdict"),
    [
        (0.20, 0.90, "silhouette"),
        (0.90, 0.30, "seed agreement"),
        (0.20, 0.30, "silhouette"),
    ],
)
def test_stability_names_what_failed(silhouette, rand, expected_in_verdict):
    check = Stability(k=3, silhouette=silhouette, adjusted_rand=rand)

    assert check.is_trustworthy is False
    assert expected_in_verdict in check.verdict


def test_measured_corpus_values_would_be_rejected():
    # The values actually observed on the 204-game corpus in Aug 2026. If a
    # future change makes these pass, it should be because the data improved.
    observed = Stability(k=3, silhouette=0.233, adjusted_rand=0.332)

    assert observed.is_trustworthy is False
