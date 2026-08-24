"""Rule-based pre-launch forecast — the Phase 1 stand-in for a trained model.

The rubric in `app/rubric.py` scores a game that has already launched. This
module answers the different question the dashboard actually asks: given a
title that has not shipped yet, what is it likely to do?

It has none of the launch-window evidence the rubric relies on, so it falls
back to the only structural signal the dataset currently supports — how the
publisher's and developer's own past day-one Steam releases turned out — with
platform reach as a weak modifier.

This is deliberately a floor, not a model. Phase 2 replaces it with a trained
ordinal classifier over a much wider feature set (company tier, franchise
history, competing releases, marketing lead time). It exists so the dashboard
has an honest prediction while the labeled set is still thin, and so there is
something for the trained model to beat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    HistoricalRelease,
    Outcome,
    PlatformLaunchType,
)

TIERS = (Outcome.FLOP, Outcome.UNDERPERFORM, Outcome.SUCCESS, Outcome.BREAKOUT)

# How many of a company's own prior releases are needed before its track
# record outweighs the dataset-wide base rate. Below this the sample says
# more about which games happen to be labeled than about the company.
MIN_TRACK_RECORD = 2

# Weight given to a company's own history once it clears MIN_TRACK_RECORD.
# Capped well below 1 because two or three prior titles is thin evidence.
MAX_HISTORY_WEIGHT = 0.6
PER_TITLE_WEIGHT = 0.2

# PC-only releases skew lower in the labeled set; a small nudge, not a verdict.
PC_ONLY_SHIFT = 0.05


@dataclass(slots=True)
class BaselineForecast:
    probabilities: dict[Outcome, float]
    predicted: Outcome
    confidence: str
    rationale: str
    basis: list[str] = field(default_factory=list)

    @property
    def predicted_probability(self) -> float:
        return self.probabilities[self.predicted]


def _normalize(weights: dict[Outcome, float]) -> dict[Outcome, float]:
    total = sum(weights.values())
    if total <= 0:
        return dict.fromkeys(TIERS, 0.25)
    return {tier: round(weights[tier] / total, 4) for tier in TIERS}


def base_rate(session: Session) -> dict[Outcome, float]:
    """Tier distribution across labeled day-one Steam releases.

    Day-one only, for the same reason the rubric validation excludes ports:
    a delayed port's label describes a launch that happened elsewhere.
    """
    rows = session.scalars(
        select(HistoricalRelease).where(
            HistoricalRelease.resolved_outcome.is_not(None),
            HistoricalRelease.platform_launch_type == PlatformLaunchType.DAY_ONE_STEAM,
        )
    ).all()

    counts = dict.fromkeys(TIERS, 0.0)
    for row in rows:
        counts[row.resolved_outcome] += 1.0
    # Laplace smoothing: no tier should ever be assigned probability zero on
    # the strength of a few dozen examples.
    return _normalize({tier: counts[tier] + 1.0 for tier in TIERS})


def company_history(
    session: Session, developer: str | None, publisher: str | None
) -> list[HistoricalRelease]:
    """Labeled day-one releases from the same developer or publisher."""
    names = {name.strip() for name in (developer or "").split("\n") if name.strip()}
    names |= {name.strip() for name in (publisher or "").split("\n") if name.strip()}
    if not names:
        return []

    rows = session.scalars(
        select(HistoricalRelease).where(
            HistoricalRelease.resolved_outcome.is_not(None),
            HistoricalRelease.platform_launch_type == PlatformLaunchType.DAY_ONE_STEAM,
        )
    ).all()

    return [row for row in rows if names & _company_names(row)]


def _company_names(row: HistoricalRelease) -> set[str]:
    out = {n.strip() for n in (row.developer or "").split("\n") if n.strip()}
    out |= {n.strip() for n in (row.publisher or "").split("\n") if n.strip()}
    return out


def forecast(
    session: Session,
    *,
    developer: str | None = None,
    publisher: str | None = None,
    on_windows: bool = True,
    on_mac: bool = False,
    on_linux: bool = False,
    exclude_appid: int | None = None,
) -> BaselineForecast:
    """Produce a pre-launch tier distribution and a plain-language rationale."""
    priors = base_rate(session)
    basis = [f"dataset base rate over {_labeled_count(session)} labeled day-one releases"]

    history = [
        row
        for row in company_history(session, developer, publisher)
        if row.steam_appid != exclude_appid
    ]

    weights = dict(priors)
    if len(history) >= MIN_TRACK_RECORD:
        weight = min(MAX_HISTORY_WEIGHT, PER_TITLE_WEIGHT * len(history))
        counts = dict.fromkeys(TIERS, 0.0)
        for row in history:
            counts[row.resolved_outcome] += 1.0
        record = _normalize({tier: counts[tier] + 0.5 for tier in TIERS})
        weights = {tier: (1 - weight) * priors[tier] + weight * record[tier] for tier in TIERS}
        names = ", ".join(sorted({row.game_name for row in history})[:3])
        basis.append(
            f"{len(history)} prior labeled release(s) from the same team ({names}"
            f"{', …' if len(history) > 3 else ''}), weighted {weight:.0%}"
        )
    elif history:
        basis.append(
            f"only {len(history)} prior labeled release from this team — too few to "
            "outweigh the base rate"
        )
    else:
        basis.append("no prior labeled releases from this developer or publisher")

    if on_windows and not (on_mac or on_linux):
        weights[Outcome.FLOP] *= 1 + PC_ONLY_SHIFT
        weights[Outcome.UNDERPERFORM] *= 1 + PC_ONLY_SHIFT
        basis.append("Windows-only on Steam, a mild negative in the labeled set")

    probabilities = _normalize(weights)
    predicted = max(TIERS, key=lambda tier: probabilities[tier])

    return BaselineForecast(
        probabilities=probabilities,
        predicted=predicted,
        # A rule-based prior over structural features is a floor, never a
        # confident call. Saying otherwise on a dashboard would be misleading.
        confidence="low",
        rationale=_rationale(predicted, probabilities, history),
        basis=basis,
    )


def _labeled_count(session: Session) -> int:
    return len(
        session.scalars(
            select(HistoricalRelease.id).where(
                HistoricalRelease.resolved_outcome.is_not(None),
                HistoricalRelease.platform_launch_type == PlatformLaunchType.DAY_ONE_STEAM,
            )
        ).all()
    )


def _rationale(
    predicted: Outcome, probabilities: dict[Outcome, float], history: list[HistoricalRelease]
) -> str:
    share = probabilities[predicted]
    label = predicted.value.replace("_", " ")
    if len(history) >= MIN_TRACK_RECORD:
        return (
            f"Baseline forecast leans {label} ({share:.0%}), driven mainly by how this "
            f"team's {len(history)} previous Steam releases resolved. This is a "
            "structural prior only — no pre-launch signals for this title have been "
            "assessed yet."
        )
    return (
        f"Baseline forecast leans {label} ({share:.0%}), from the dataset-wide "
        "distribution of past releases. With no track record for this team in the "
        "labeled set, this carries little information about this specific title."
    )
