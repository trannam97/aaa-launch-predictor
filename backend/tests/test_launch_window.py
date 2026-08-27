"""Tests for deciding when a launch actually began.

The rule is that a launch is the 1.0 version. A premium edition unlocking
early ships 1.0 and counts; an Early Access build is not 1.0 and does not.
Getting this backwards in either direction is silent: shifting an Early
Access game pulls unfinished-build reviews into its launch window, and not
shifting a head-start game throws away a third of its launch.
"""

from __future__ import annotations

from datetime import date

from app.launch_window import MEANINGFUL_REVIEWS, LaunchStart, detect
from app.steam import ReviewSummary

RECORDED = date(2024, 9, 9)


class FakeClient:
    """Answers review queries from a per-day script."""

    def __init__(self, per_day: dict[date, int]):
        self.per_day = per_day
        self.calls = 0

    def get_review_summary(self, appid, start=None, end=None):
        self.calls += 1
        if start is None or end is None:
            return ReviewSummary(total=sum(self.per_day.values()))
        # Mirror the real client's contract, which rejects a zero-width window.
        # An earlier version of this fake accepted start == end and hid a bug
        # that only surfaced against live Steam.
        if end <= start:
            raise ValueError("window_end must be after window_start")
        total = sum(n for day, n in self.per_day.items() if start.date() <= day < end.date())
        return ReviewSummary(total=total)


def days_before(offset: int) -> date:
    return date.fromordinal(RECORDED.toordinal() - offset)


def test_a_premium_head_start_moves_the_launch_earlier():
    # Space Marine 2's shape: nothing for months, then a large burst four days
    # before the store date when the Gold edition unlocked.
    client = FakeClient({days_before(o): 5000 for o in (1, 2, 3, 4)})

    result = detect(client, 1, RECORDED)

    assert result.shifted
    assert result.detected == days_before(4)
    assert result.days_earlier == 4
    assert "1.0 on sale" in result.reason


def test_an_early_access_tail_leaves_the_date_alone():
    # Baldur's Gate 3's shape: steady sales for months before 1.0. Those are
    # reviews of an unfinished build and must stay outside the launch window.
    client = FakeClient({days_before(o): 200 for o in range(1, 90)})

    result = detect(client, 1, RECORDED)

    assert not result.shifted
    assert result.detected == RECORDED
    assert "Early Access" in result.reason


def test_a_clean_launch_is_left_alone():
    result = detect(FakeClient({}), 1, RECORDED)

    assert not result.shifted
    assert result.reason == "no head start found"


def test_a_trickle_of_early_reviews_is_not_a_head_start():
    # A handful of reviews before release is noise — an import, a regional
    # rollout, a press copy — not evidence the game went on sale.
    client = FakeClient({days_before(3): MEANINGFUL_REVIEWS - 1})

    assert not detect(client, 1, RECORDED).shifted


def test_early_access_wins_even_when_a_head_start_burst_exists():
    # A game can have both: a long Early Access tail and a launch-week surge.
    # Early Access is decisive, because shifting would pull in the tail.
    per_day = {days_before(o): 300 for o in range(15, 90)}
    per_day.update({days_before(o): 9000 for o in (1, 2, 3)})
    client = FakeClient(per_day)

    result = detect(client, 1, RECORDED)

    assert not result.shifted
    assert "Early Access" in result.reason


def test_the_head_start_search_stops_at_the_bound():
    # Reviews every day for a fortnight, with nothing before, is still treated
    # as a head start bounded at fourteen days rather than an open-ended walk.
    client = FakeClient({days_before(o): 400 for o in range(1, 15)})

    result = detect(client, 1, RECORDED)

    assert result.detected == days_before(14)


def test_launch_start_reports_no_shift_when_dates_match():
    unchanged = LaunchStart(RECORDED, RECORDED, 0, 0, "no head start found")

    assert not unchanged.shifted
    assert unchanged.days_earlier == 0
