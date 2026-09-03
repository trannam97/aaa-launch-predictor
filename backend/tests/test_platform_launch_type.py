"""The launch type is derived from two dates, and derivation must stop where
the dates stop being decisive.

`delayed_port` and `former_exclusive` are indistinguishable as a gap: both are
"Steam got it later". Filling one in from a date difference would put a guess
into a curated column, which is the failure this job exists to correct rather
than repeat. So the tests here pin two things — the day-one case is decided,
and everything else is handed over with its evidence and an empty proposal.
"""

from __future__ import annotations

import ast
import csv
import inspect
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))

import backfill_platform_launch_type as job  # noqa: E402

from app.models import PORT_GAP_TOLERANCE_DAYS, PlatformLaunchType  # noqa: E402


def code_of(func) -> str:
    """The function's code with its docstring and comments stripped.

    These tests assert that a wrong construct is absent, and both functions
    below explain that construct in prose. Reading the raw source would fail on
    the explanation of the very mistake being guarded against.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    definition = tree.body[0]
    if ast.get_docstring(definition):
        definition.body = definition.body[1:]
    return ast.unparse(definition)


def test_dates_that_agree_are_day_one():
    verdict, proposed, gap, _ = job.classify(date(2021, 5, 14), date(2021, 5, 14))
    assert verdict == "day_one_steam"
    assert proposed == PlatformLaunchType.DAY_ONE_STEAM.value
    assert gap == 0


def test_a_worldwide_rollout_crossing_midnight_is_still_day_one():
    """Steam holds one date for a global release, so a launch straddling
    timezones lands a day either side. Cyberpunk 2077, Sekiro, Dying Light 2
    and Beyond Good & Evil all differ from their curated date by exactly one
    day and none of them is a port. 142 of the 206 rows sit in this band."""
    for steam in (date(2020, 12, 9), date(2020, 12, 11)):
        verdict, proposed, _, _ = job.classify(date(2020, 12, 10), steam)
        assert verdict == "day_one_steam"
        assert proposed == PlatformLaunchType.DAY_ONE_STEAM.value


def test_a_later_steam_listing_is_reported_but_not_named():
    """The whole point: a gap proves Steam was late, and proves nothing about
    why. Horizon Zero Dawn was console-first (`delayed_port`); Control was an
    Epic exclusive (`former_exclusive`). Same shape, different answer."""
    verdict, proposed, gap, note = job.classify(date(2017, 2, 28), date(2020, 8, 7))
    assert verdict == "not_day_one"
    assert proposed == ""
    assert gap == 1256
    assert PlatformLaunchType.DELAYED_PORT.value in note
    assert PlatformLaunchType.FORMER_EXCLUSIVE.value in note


def test_the_twelve_audited_rows_all_read_as_not_day_one():
    """Measured against live Steam listings. These are the unambiguous end of
    the range, 264 days and up, so no plausible threshold reaches them."""
    audited = [
        (date(2014, 11, 11), date(2019, 12, 3)),  # Halo: The Master Chief Collection
        (date(2021, 9, 23), date(2026, 2, 11)),  # Diablo II: Resurrected
        (date(2017, 2, 28), date(2020, 8, 7)),  # Horizon Zero Dawn
        (date(2017, 3, 21), date(2020, 6, 11)),  # Mass Effect: Andromeda
        (date(2018, 3, 20), date(2020, 6, 3)),  # Sea of Thieves
        (date(2018, 5, 22), date(2020, 3, 13)),  # State of Decay 2
        (date(2020, 11, 12), date(2022, 8, 12)),  # Marvel's Spider-Man Remastered
        (date(2023, 10, 20), date(2025, 1, 30)),  # Marvel's Spider-Man 2
        (date(2019, 8, 27), date(2020, 8, 27)),  # Control
        (date(2020, 3, 13), date(2021, 2, 5)),  # Nioh 2
        (date(2018, 11, 13), date(2019, 9, 3)),  # Spyro Reignited Trilogy
        (date(2022, 1, 28), date(2022, 10, 19)),  # Uncharted: Legacy of Thieves
    ]
    for original, steam in audited:
        verdict, proposed, gap, _ = job.classify(original, steam)
        assert verdict == "not_day_one", (original, steam)
        assert proposed == ""
        assert gap is not None and gap >= 264


def test_a_gap_too_big_for_a_timezone_and_too_small_for_a_port_is_undecided():
    """The band the corpus actually occupies between the two clusters: nine
    rows from 3 to 26 days. Watch_Dogs 2 (+13), NieR:Automata (+22) and
    Assassin's Creed Syndicate (+26) are console-first PC releases where Steam
    buyers arrived after reviews existed elsewhere; No Man's Sky (+3) and
    Starfield (+4) are staggered launches of one release. The dates cannot
    tell those apart, so neither does this job."""
    for gap_days, name in ((3, "No Man's Sky"), (13, "Watch_Dogs 2"), (26, "AC Syndicate")):
        verdict, proposed, gap, note = job.classify(
            date(2020, 1, 1), date(2020, 1, 1) + timedelta(days=gap_days)
        )
        assert verdict == "near_day_one", name
        assert proposed == "", name
        assert gap == gap_days
        assert "timezone" in note


def test_an_early_access_graduation_is_not_mistaken_for_a_port():
    """The failure this guard exists for. Steam reports a graduated title's 1.0
    date while `original_release_date` holds the Early Access start, so Grounded
    reads as 791 days late and Starship Troopers: Extermination as 512 -- the
    exact shape of a delayed port. Both are marked day_one_steam in the corpus
    under its launch-is-1.0 rule, so `not_day_one` here would contradict a
    documented project rule with a confident-looking verdict."""
    for original, steam, name in (
        (date(2020, 7, 28), date(2022, 9, 27), "Grounded"),
        (date(2023, 5, 17), date(2024, 10, 10), "Starship Troopers: Extermination"),
        (date(2024, 1, 19), date(2025, 1, 1), "Palworld"),
    ):
        verdict, proposed, _, note = job.classify(
            original, steam, f"EARLY ACCESS GRADUATION: Steam Early Access from {original}"
        )
        assert verdict == "early_access", name
        assert proposed == "", name
        assert "launch-is-1.0" in note


def test_the_same_gap_without_the_marker_is_still_a_port():
    """The exception is driven by the curated marker, not by the size of the
    gap -- otherwise it would swallow the twelve real ports."""
    verdict, _, _, _ = job.classify(date(2017, 2, 28), date(2020, 8, 7), "Wikidata P577")
    assert verdict == "not_day_one"
    verdict, _, _, _ = job.classify(date(2017, 2, 28), date(2020, 8, 7), None)
    assert verdict == "not_day_one"


def test_a_port_note_names_early_access_as_a_possibility():
    """A reviewer picking between delayed_port and former_exclusive on an
    unmarked row needs to know there is a third answer."""
    _, _, _, note = job.classify(date(2017, 2, 28), date(2020, 8, 7))
    assert "Early Access" in note


def test_the_two_thresholds_mean_different_things():
    """One day is what a worldwide rollout costs; thirty is where a gap starts
    moving a 16-month research window. Collapsing them would either call a
    26-day console-first launch day-one, or drop 142 clean rows into review."""
    assert job.DAY_ONE_TOLERANCE_DAYS == 1
    assert job.PORT_TOLERANCE_DAYS == PORT_GAP_TOLERANCE_DAYS


def test_steam_predating_the_original_release_is_flagged_not_classified():
    """`original_release_date` is the earliest publication anywhere, so Steam
    cannot beat it. When it does, a date is wrong and neither is trustworthy."""
    verdict, proposed, gap, note = job.classify(date(2020, 1, 1), date(2019, 1, 1))
    assert verdict == "steam_first"
    assert proposed == ""
    assert gap == -365
    assert "should not happen" in note


def test_a_missing_date_names_which_one_is_missing():
    for original, steam, expected in (
        (None, date(2020, 1, 1), "original_release_date"),
        (date(2020, 1, 1), None, "steam_release_date"),
        (None, None, "original_release_date"),
    ):
        verdict, proposed, gap, note = job.classify(original, steam)
        assert verdict == "no_date"
        assert proposed == ""
        assert gap is None
        assert expected in note


def test_the_queue_matches_on_unknown_not_on_null():
    """`platform_launch_type` is nullable=False with default UNKNOWN, so an
    unanswered row holds UNKNOWN. A `.is_(None)` filter matches nothing and
    returns the whole corpus instead -- the same mistake against this schema
    spent $25 researching 206 rows that were already answered."""
    source = code_of(job.needs_launch_type)
    assert "PlatformLaunchType.UNKNOWN" in source
    assert "is_(None)" not in source


def test_apply_writes_only_the_decided_verdict():
    """An ambiguous row must not reach the database however the flags are set."""
    source = code_of(job.main).replace("'", '"')  # ast.unparse normalises quotes
    assert 'verdict == "day_one_steam"' in source
    assert "PlatformLaunchType.DAY_ONE_STEAM" in source
    for guessed in ("PlatformLaunchType.DELAYED_PORT", "PlatformLaunchType.FORMER_EXCLUSIVE"):
        assert guessed not in source


def test_applying_reports_what_it_just_made_billable():
    """`platform_launch_type` gates the signal-drafts queue: only day_one_steam
    rows are researched. Filling the column in therefore enlarges a paid queue,
    and a run that reported only "applied to N rows" would hide that."""
    source = code_of(job.main)
    assert "eligible for signal research" in source
    assert "0.21" in source and "0.34" in source


def test_proposals_round_trip(tmp_path):
    path = tmp_path / "proposals.csv"
    rows = {
        1: {
            "steam_appid": 1,
            "game_name": "Game 1",
            "verdict": "not_day_one",
            "platform_launch_type": "",
            "original_release_date": "2017-02-28",
            "steam_release_date": "2020-08-07",
            "gap_days": 1256,
            "note": "Steam listing arrives 1256 days later.",
        }
    }
    job.write_proposals(path, rows)
    assert job.read_proposals(path)[1]["gap_days"] == "1256"
    with path.open(newline="") as handle:
        assert next(csv.reader(handle)) == job.FIELDS


def test_reading_back_an_absent_file_is_empty_not_an_error(tmp_path):
    assert job.read_proposals(tmp_path / "nope.csv") == {}
