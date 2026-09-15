"""The validation job reads the database; the corpus lives in a CSV.

Those two only meet when `backfill` runs, so a CSV edit followed straight by a
validation run scores the *previous* corpus — and the old job said nothing
about it, printing a clean report with a stale headline. That happened: a
support_signal recode sat in the CSV through two full validation runs while the
report kept quoting the pre-recode number. These tests pin the guard that makes
the drift impossible to miss, and the exit-code rule that stops CI gating on it.

Also pinned: the going-rate note is derived from the table it sits under. It
used to be a constant, and drifted — claiming $60 held "through 2022" while the
rows printed directly above it put the change at 2024.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))

import validate_rubric as job  # noqa: E402

from app.models import (  # noqa: E402
    HistoricalRelease,
    LabelConfidence,
    Outcome,
    PlatformLaunchType,
    ResearchStatus,
    StudioSignal,
    SupportSignal,
)

CSV_HEADER = (
    "steam_appid,game_name,original_release_date,platform_launch_type,platform_reach,"
    "budget_tier,launch_price_usd,post_launch_support,studio_outcome,studio_signal,"
    "support_signal,resolved_outcome,label_confidence,research_status,notes,sources"
)


def write_csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "historical_releases.csv"
    path.write_text("\r\n".join((CSV_HEADER, *rows)) + "\r\n", encoding="utf-8")
    return path


def labeled_row(
    appid: int = 1,
    *,
    support: str = "sustained",
    outcome: str = "success",
    confidence: str = "high",
) -> str:
    return (
        f"{appid},Game {appid},2024-01-01,day_one_steam,PC,aaa,69.99,,,"
        f"continued,{support},{outcome},{confidence},researched,,"
    )


def add_release(session, appid: int = 1, **overrides) -> HistoricalRelease:
    fields = {
        "steam_appid": appid,
        "game_name": f"Game {appid}",
        "steam_release_date": date(2024, 1, 1),
        "original_release_date": date(2024, 1, 1),
        "cohort_year": 2024,
        "platform_launch_type": PlatformLaunchType.DAY_ONE_STEAM,
        "studio_signal": StudioSignal.CONTINUED,
        "support_signal": SupportSignal.SUSTAINED,
        "resolved_outcome": Outcome.SUCCESS,
        "label_confidence": LabelConfidence.HIGH,
        "research_status": ResearchStatus.RESEARCHED,
    }
    fields.update(overrides)
    release = HistoricalRelease(**fields)
    session.add(release)
    session.flush()
    return release


@pytest.fixture
def curated(tmp_path, monkeypatch):
    """Point the job's CURATED_CSV at a temp file for the duration of a test."""

    def use(*rows: str) -> Path:
        path = write_csv(tmp_path, *rows)
        monkeypatch.setattr(job, "CURATED_CSV", path)
        return path

    return use


class TestDrift:
    def test_a_matching_database_reports_no_drift(self, session, curated):
        curated(labeled_row())
        add_release(session)
        assert job._curated_drift(session) == []

    def test_a_recode_that_has_not_been_backfilled_is_reported(self, session, curated):
        """The exact case that went unnoticed: CSV recoded, database not."""
        curated(labeled_row(support="abandoned"))
        add_release(session, support_signal=SupportSignal.CURTAILED)

        assert job._curated_drift(session) == [
            ("Game 1", "support_signal", "curtailed", "abandoned")
        ]

    def test_every_label_bearing_field_is_watched(self, session, curated):
        curated(labeled_row(support="abandoned", outcome="flop", confidence="medium"))
        add_release(
            session,
            support_signal=SupportSignal.CURTAILED,
            resolved_outcome=Outcome.UNDERPERFORM,
            label_confidence=LabelConfidence.HIGH,
        )

        assert {field for _, field, _, _ in job._curated_drift(session)} == {
            "support_signal",
            "resolved_outcome",
            "label_confidence",
        }

    def test_a_labeled_row_missing_from_the_database_is_drift(self, session, curated):
        """A row the backfill never created is a row the score silently omits."""
        curated(labeled_row(appid=1), labeled_row(appid=2))
        add_release(session, appid=1)

        assert job._curated_drift(session) == [("appid 2", "row", "absent", "labeled")]

    def test_unlabeled_rows_are_ignored(self, session, curated):
        """Only labeled rows reach the score, so only they can make it stale."""
        curated("7,Unlabeled,2024-01-01,day_one_steam,PC,aaa,69.99,,,,,,,not_researched,,")
        assert job._curated_drift(session) == []

    def test_prose_edits_are_not_drift(self, session, curated):
        """A reworded note changes nothing the rubric reads."""
        row = labeled_row() + '"a much longer note","and new sources"'
        curated(row)
        add_release(session, notes="the old note")
        assert job._curated_drift(session) == []

    def test_an_unreadable_csv_does_not_withhold_the_numbers(self, session, monkeypatch):
        monkeypatch.setattr(job, "CURATED_CSV", Path("/nonexistent/historical_releases.csv"))
        assert job._curated_drift(session) == []


class TestDriftBanner:
    def test_silent_when_fresh(self, capsys):
        job._print_drift([])
        assert capsys.readouterr().out == ""

    def test_names_the_row_the_field_and_both_values(self, capsys):
        job._print_drift([("Suicide Squad", "support_signal", "curtailed", "abandoned")])
        out = capsys.readouterr().out

        assert "THE DATABASE IS STALE" in out
        assert "Run backfill" in out
        for fragment in ("Suicide Squad", "support_signal", "db=curtailed", "csv=abandoned"):
            assert fragment in out

    def test_says_the_numbers_below_are_about_the_database(self, capsys):
        """The banner has to explain what is wrong, not just that something is."""
        job._print_drift([("Game", "resolved_outcome", "success", "flop")])
        assert "not the corpus on disk" in capsys.readouterr().out


class TestPriceNote:
    def test_describes_the_corpus_as_it_actually_stands(self):
        rates = [(y, 6000) for y in range(2014, 2024)] + [(y, 7000) for y in range(2024, 2027)]
        assert job._price_note(rates) == (
            "Nominal price is not comparable across these — the going rate was "
            "$60 through 2023, then $70 from 2024 to 2026."
        )

    def test_the_change_year_follows_the_table_not_a_constant(self):
        """The old hardcoded line said 2023; the data says whatever the data says."""
        early = job._price_note([(2014, 6000), (2015, 6000), (2016, 7000)])
        late = job._price_note([(2014, 6000), (2015, 6000), (2016, 6000), (2017, 7000)])

        assert "through 2015, then $70 from 2016" in early
        assert "through 2016, then $70 from 2017" in late

    def test_a_flat_table_does_not_claim_a_change(self):
        assert job._price_note([(2014, 6000), (2015, 6000)]) == (
            "The going rate held at $60 across every cohort here."
        )

    def test_a_single_cohort_is_flat(self):
        assert "held at $70" in job._price_note([(2024, 7000)])

    def test_more_than_one_change_is_described_in_full(self):
        note = job._price_note([(2014, 6000), (2019, 7000), (2020, 7000), (2024, 8000)])
        assert note.endswith("$60 through 2018, then $70 from 2019 to 2023, then $80 from 2024.")


class FakeReport:
    """Enough of ValidationReport for main() to print. The numbers are
    deliberately perfect, so any non-zero exit is the drift guard's doing."""

    scored = resolved = [object()]
    unresolved: list = []
    excluded: list = []
    confusion: dict = {}
    disagreements: list = []
    met_expectations_agreement = 100.0
    exact_agreement = 100.0
    mean_tier_distance = 0.0


@pytest.fixture
def run_job(session, monkeypatch):
    """Drive main() against the test session with a stubbed report."""

    @contextmanager
    def fake_scope():
        yield session

    monkeypatch.setattr(job, "session_scope", fake_scope)
    monkeypatch.setattr(job, "validate", lambda _session: FakeReport())
    return job.main


class TestExitCode:
    """A stale database must never be gated on: the number looks fine and is
    about the wrong corpus. Without a gate there is nothing to protect, so the
    banner alone stands and the run still reports."""

    def test_stale_and_gated_fails_despite_perfect_agreement(
        self, session, curated, run_job, capsys
    ):
        curated(labeled_row(support="abandoned"))
        add_release(session, support_signal=SupportSignal.CURTAILED)

        assert run_job(["--min-agreement", "90"]) == 1
        assert "run backfill before gating" in capsys.readouterr().out

    def test_stale_and_ungated_still_reports_but_warns(self, session, curated, run_job, capsys):
        curated(labeled_row(support="abandoned"))
        add_release(session, support_signal=SupportSignal.CURTAILED)

        assert run_job([]) == 0
        out = capsys.readouterr().out
        assert "THE DATABASE IS STALE" in out
        assert "Rubric validation" in out

    def test_fresh_and_gated_passes(self, session, curated, run_job, capsys):
        curated(labeled_row())
        add_release(session)

        assert run_job(["--min-agreement", "90"]) == 0
        assert "STALE" not in capsys.readouterr().out

    def test_the_agreement_gate_still_bites_on_a_fresh_database(
        self, session, curated, run_job, monkeypatch
    ):
        """The drift guard must not shadow the check it sits in front of."""
        curated(labeled_row())
        add_release(session)
        monkeypatch.setattr(FakeReport, "met_expectations_agreement", 50.0)

        assert run_job(["--min-agreement", "90"]) == 1

    def test_the_check_can_be_waived(self, session, curated, run_job, capsys):
        curated(labeled_row(support="abandoned"))
        add_release(session, support_signal=SupportSignal.CURTAILED)

        assert run_job(["--min-agreement", "90", "--no-drift-check"]) == 0
        assert "STALE" not in capsys.readouterr().out

    def test_drift_reaches_json_output(self, session, curated, run_job, capsys):
        curated(labeled_row(support="abandoned"))
        add_release(session, support_signal=SupportSignal.CURTAILED)

        run_job(["--json"])
        payload = json.loads(capsys.readouterr().out)

        assert payload["curated_drift"] == [
            {"game": "Game 1", "field": "support_signal", "db": "curtailed", "csv": "abandoned"}
        ]
