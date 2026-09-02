"""The proposals file is a queue's worth of progress, so it must survive a run.

ITAD's allowance does not stretch to 170 games. The first full attempt got 50
through, took HTTP 429 for the other 120, and reported "120 failed" — the 50
good rows survived only because a 429 happened to be caught per row. These tests
pin the two properties that make a partial run useful instead: what was gathered
is written even when the run stops, and the next run continues from it.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))

import backfill_launch_prices as job  # noqa: E402

from app.itad import LaunchPrice  # noqa: E402


def a_row(appid: int, verdict: str = "launch_price") -> dict[str, object]:
    return {
        "steam_appid": appid,
        "game_name": f"Game {appid}",
        "verdict": verdict,
        "launch_price_usd": "59.99",
        "observed_on": "2023-10-05",
        "days_after_release": -30,
        "steam_release_date": "2023-11-04",
        "note": "pre-order listing, 30d before release",
    }


def test_a_written_file_reads_back_unchanged(tmp_path):
    """The round trip is what the next run depends on."""
    out = tmp_path / "proposals.csv"
    written = {570: a_row(570), 440: a_row(440, "too_late")}

    job.write_proposals(out, written)
    read_back = job.read_proposals(out)

    assert set(read_back) == {440, 570}
    assert read_back[440]["verdict"] == "too_late"
    assert read_back[570]["launch_price_usd"] == "59.99"


def test_a_missing_file_is_an_empty_start_not_an_error():
    """The first run has nothing to resume from, and that is normal."""
    assert job.read_proposals(Path("/nonexistent/proposals.csv")) == {}


def test_rows_without_a_usable_appid_are_skipped_not_fatal(tmp_path):
    """A hand-edited file must not take the next run down with it."""
    out = tmp_path / "proposals.csv"
    out.write_text(
        "steam_appid,game_name,verdict\n"
        ",No appid,launch_price\n"
        "not-a-number,Bad appid,launch_price\n"
        "570,Fine,launch_price\n"
    )

    assert set(job.read_proposals(out)) == {570}


def test_the_file_is_sorted_so_a_resumed_run_diffs_cleanly(tmp_path):
    """Rows arrive in release order across several runs; the file must not
    reorder itself every time or every diff is the whole file."""
    out = tmp_path / "proposals.csv"

    job.write_proposals(out, {440: a_row(440), 570: a_row(570), 10: a_row(10)})

    appids = [line.split(",")[0] for line in out.read_text().splitlines()[1:]]
    assert appids == ["10", "440", "570"]


@pytest.mark.parametrize(
    ("gap", "suspect", "expected"),
    [
        (-30, False, "launch_price"),
        (1500, False, "too_late"),
        (0, True, "suspect_shape"),
    ],
)
def test_the_verdict_is_the_triage(gap, suspect, expected):
    """`launch_price` can be copied across; the others need a human."""
    found = LaunchPrice(
        price_cents=5999,
        observed_on=date(2023, 10, 5),
        days_after_release=gap,
        coverage="parsed 1 of 9 history entries",
        suspect_shape=suspect,
    )

    assert job.as_row(570, "Game", date(2023, 11, 4), found)["verdict"] == expected
