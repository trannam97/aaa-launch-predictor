"""A units-sold figure is evidence for a reviewer, never an input to the rubric.

The three reasons are structural rather than stylistic, so the tests pin them:
the figures are worldwide across all platforms, they exist only because a
publisher chose to announce them, and their absence means different things at
the two ends of the sentiment range. A job that quietly wrote them into the
database, or a rubric that read them, would import a one-directional evidence
channel into a measurement.
"""

from __future__ import annotations

import ast
import csv
import inspect
import sys
import textwrap
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "jobs"))

import enrich_sales_milestones as job  # noqa: E402

from app.wikidata import SalesHistory, SalesMilestone, _sales_query  # noqa: E402

LAUNCH = date(2024, 1, 25)
TEKKEN = SalesHistory(
    1778820,
    [
        SalesMilestone(2_000_000, date(2024, 2, 15)),
        SalesMilestone(3_000_000, date(2025, 2, 24)),
    ],
)


class Release:
    def __init__(self, appid=1778820, name="TEKKEN 8", launch=LAUNCH):
        self.steam_appid, self.game_name, self.steam_release_date = appid, name, launch


def test_the_earliest_figure_after_launch_is_the_one_that_reads_as_a_launch():
    """A later milestone describes a long tail. TEKKEN 8 holds 2M at 21 days and
    3M thirteen months out; only the first says anything about the launch."""
    first = TEKKEN.first_after(LAUNCH)
    assert first is not None
    assert (first.units, first.as_of) == (2_000_000, date(2024, 2, 15))


def test_a_figure_predating_the_launch_is_not_a_launch_figure():
    """Console-first titles carry figures from before the Steam release. Those
    describe a different launch on a different platform."""
    history = SalesHistory(1, [SalesMilestone(23_000_000, date(2019, 2, 6))])
    assert history.first_after(date(2019, 12, 5)) is None


def test_a_coarse_date_reads_as_undated_rather_than_as_january_first():
    """The year-precision trap, already paid for once on P577: Wikidata renders
    a year-precision value as January 1st, which compared as a day put Uncharted
    on PC nine months early. Here it would date a milestone into a launch window
    it never belonged to, so anything coarser than a day drops `as_of`."""
    source = inspect.getsource(sys.modules["app.wikidata"].WikidataClient.sales_milestones)
    assert "precision < DAY_PRECISION" in source
    assert "as_of = None" in source


def test_an_undated_milestone_is_kept_not_dropped():
    """It cannot answer a launch-window question, but a reviewer with no dated
    figure is better off seeing "10 million, undated" than an empty row."""
    history = SalesHistory(1, [SalesMilestone(10_000_000, None)])
    assert history.first_after(LAUNCH) is None
    row = job.as_row(Release(), history)
    assert "undated" in str(row["all_milestones"])
    assert "1 undated milestone" in str(row["note"])


def test_a_row_with_no_figure_says_absence_is_not_evidence():
    """The finding this job exists to stop being misread. Measured over the
    68-row queue, the share carrying a figure is not monotonic in sentiment:
    12% under 50%, 57% at 60-69%, back to 26% among the volume-floor failures.
    Below 50% the game did badly; above 78% it is segment -- Game Pass titles,
    niche PC genres and remasters rarely get a unit milestone however well they
    did."""
    note = str(job.as_row(Release(), None)["note"])
    assert "not evidence of a weak launch" in note
    assert job.as_row(Release(), None)["units_at_launch"] == ""


def test_a_long_tail_figure_is_flagged_as_one():
    history = SalesHistory(1, [SalesMilestone(35_000_000, date(2026, 7, 17))])
    row = job.as_row(Release(launch=date(2015, 11, 9)), history)
    assert row["within_launch_window"] == "no"
    assert "long tail, not a launch" in str(row["note"])


def test_every_populated_row_carries_the_scope_caveat():
    """Worldwide across all platforms. A reviewer reading 2,000,000 beside a
    Steam-scoped rubric verdict has to be told those are different quantities,
    on the row rather than in a docstring they will not open."""
    assert "not Steam-specific" in str(job.as_row(Release(), TEKKEN)["note"])


def test_the_job_never_writes_the_database():
    """Publishers announce milestones when a game does well, so this source can
    only ever upgrade a row. Wired to the database it would be a one-directional
    thumb on the scale; as a review file it is evidence a person weighs."""
    source = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(job))))
    for forbidden in ("session.commit", "session.add", "resolved_outcome ="):
        assert forbidden not in source
    assert not hasattr(job.parse_args([]), "apply")


def test_the_query_follows_p629_and_drops_deprecated_statements():
    """Same two rules as the release-date query: a Complete Edition item rarely
    carries its own sales, and a deprecated statement is one Wikidata already
    decided against."""
    q = _sales_query([1, 2])
    assert "wdt:P629" in q
    assert "DeprecatedRank" in q
    assert "pqv:P585" in q, "the point in time is the whole value of the figure"


def test_the_review_file_round_trips(tmp_path):
    path = tmp_path / "sales.csv"
    job.write_rows(path, [job.as_row(Release(), TEKKEN)])
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == job.FIELDS
        assert next(reader)["units_at_launch"] == "2000000"
