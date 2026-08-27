"""When did the launch actually start?

Every windowed metric is measured from a start date, and Steam's own release
date is the wrong one surprisingly often. Two distinct things go wrong, and
they need opposite treatment:

**A premium edition unlocking early.** Deluxe and Gold tiers now routinely
unlock three to five days ahead of the standard edition, and Steam's release
date is the *standard* date. Those buyers are playing the finished 1.0 build
and reviewing it, inside a window we would otherwise exclude. Warhammer 40,000:
Space Marine 2 has 23,194 such reviews — more than a third of its launch
fortnight, sitting outside it.

**An Early Access period.** Baldur's Gate 3 was purchasable for nearly three
years before 1.0. Those reviews are of an unfinished build, and the launch
window correctly opens at 1.0, not at Early Access.

The project's rule is that **the launch is the 1.0 version**. A premium head
start ships 1.0, so it counts; an Early Access build is not 1.0, so it does
not. Distinguishing them is what this module does, using the one property that
separates them reliably: an Early Access tail runs for months, a head start
for days.

Two probes decide it. If the game was already selling well before the recorded
date, it had an Early Access period and the date stands. If it was not, but
reviews appear in the days just before, that is a head start and the window
opens there instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.steam import SteamClient

# How far back a head start could plausibly reach. Premium tiers run three to
# five days; two weeks is generous without straying into Early Access.
HEAD_START_MAX_DAYS = 14

# The Early Access probe: from three months before the recorded date up to the
# edge of the head-start window. Meaningful sales there mean the game was
# purchasable long before 1.0.
EARLY_ACCESS_LOOKBACK_DAYS = 90

# Below this, a handful of reviews is noise — a stray import, a regional
# rollout, a reviewer with early access from the publisher. Above it, people
# were buying.
MEANINGFUL_REVIEWS = 50


@dataclass(slots=True)
class LaunchStart:
    """What the probes concluded, and why."""

    recorded: date
    detected: date
    early_access_reviews: int
    head_start_reviews: int
    reason: str

    @property
    def shifted(self) -> bool:
        return self.detected != self.recorded

    @property
    def days_earlier(self) -> int:
        return (self.recorded - self.detected).days


def _as_utc(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def detect(client: SteamClient, appid: int, recorded: date) -> LaunchStart:
    """Find the first day the 1.0 build was on sale.

    Returns the recorded date unchanged whenever the evidence does not clearly
    say otherwise — an ambiguous case keeps the date Steam gave us rather than
    inventing a better one.
    """
    early = client.get_review_summary(
        appid,
        _as_utc(recorded - timedelta(days=EARLY_ACCESS_LOOKBACK_DAYS)),
        _as_utc(recorded - timedelta(days=HEAD_START_MAX_DAYS + 1)),
    )
    early_total = early.total or 0

    head = client.get_review_summary(
        appid,
        _as_utc(recorded - timedelta(days=HEAD_START_MAX_DAYS)),
        _as_utc(recorded - timedelta(days=1)),
    )
    head_total = head.total or 0

    if early_total >= MEANINGFUL_REVIEWS:
        return LaunchStart(
            recorded,
            recorded,
            early_total,
            head_total,
            "sold well before 1.0 — Early Access, date stands",
        )
    if head_total < MEANINGFUL_REVIEWS:
        return LaunchStart(recorded, recorded, early_total, head_total, "no head start found")

    # Walk back day by day to the first with any reviews at all. The cumulative
    # probe proved something is there; this finds where it begins.
    earliest = recorded
    for offset in range(1, HEAD_START_MAX_DAYS + 1):
        day = recorded - timedelta(days=offset)
        # A single day is [00:00, next 00:00); the client requires end > start.
        next_day = _as_utc(day + timedelta(days=1))
        if (client.get_review_summary(appid, _as_utc(day), next_day).total or 0) > 0:
            earliest = day
    return LaunchStart(
        recorded,
        earliest,
        early_total,
        head_total,
        f"1.0 on sale {(recorded - earliest).days} day(s) before the store date",
    )
