"""Tests for the Phase 0.5 historical backfill."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.backfill import (
    CuratedCsvError,
    CuratedRelease,
    backfill_release,
    derive_platform_launch_type,
    load_curated_csv,
)
from app.models import (
    BudgetTier,
    HistoricalRelease,
    LabelConfidence,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    ResearchStatus,
    WindowKey,
)
from app.steam import SteamClient
from tests.conftest import RELEASED_APPID, load_fixture

CSV_HEADER = (
    "steam_appid,game_name,original_release_date,platform_launch_type,platform_reach,"
    "budget_tier,launch_price_usd,post_launch_support,studio_outcome,resolved_outcome,"
    "label_confidence,research_status,notes,sources\n"
)


# --- platform launch type -------------------------------------------------


@pytest.mark.parametrize(
    ("steam", "original", "expected"),
    [
        (date(2024, 8, 23), date(2024, 8, 23), PlatformLaunchType.DAY_ONE_STEAM),
        (date(2024, 8, 25), date(2024, 8, 23), PlatformLaunchType.DAY_ONE_STEAM),
        (date(2019, 12, 5), date(2018, 10, 26), PlatformLaunchType.DELAYED_PORT),
        (date(2024, 3, 1), date(2024, 1, 1), PlatformLaunchType.DELAYED_PORT),
        (None, date(2024, 1, 1), PlatformLaunchType.UNKNOWN),
        (date(2024, 1, 1), None, PlatformLaunchType.UNKNOWN),
        # Steam earlier than the recorded original means the curated date is
        # wrong — say unknown rather than invent a classification.
        (date(2023, 1, 1), date(2024, 1, 1), PlatformLaunchType.UNKNOWN),
    ],
)
def test_derive_platform_launch_type(steam, original, expected):
    assert derive_platform_launch_type(steam, original) is expected


# --- CSV loading ----------------------------------------------------------


def write_csv(tmp_path, body: str):
    path = tmp_path / "curated.csv"
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def test_load_curated_csv(tmp_path):
    path = write_csv(
        tmp_path,
        "2443720,Concord,2024-08-23,,PC+PS5,aaa,40,Shut down,Studio closed,flop,high,"
        "researched,Lowest peak CCU tracked,bo3.gg\n"
        "1245620,ELDEN RING,,,,unknown,,,,,,not_researched,,\n",
    )

    rows = load_curated_csv(path)

    assert len(rows) == 2
    concord, elden = rows
    assert concord.steam_appid == 2443720
    assert concord.original_release_date == date(2024, 8, 23)
    assert concord.budget_tier is BudgetTier.AAA
    assert concord.launch_price_cents == 4000
    assert concord.resolved_outcome is Outcome.FLOP
    assert concord.label_confidence is LabelConfidence.HIGH
    assert concord.research_status is ResearchStatus.RESEARCHED
    assert elden.resolved_outcome is None
    assert elden.research_status is ResearchStatus.NOT_RESEARCHED
    assert elden.launch_price_cents is None


def test_load_curated_csv_rejects_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("steam_appid,game_name\n1,Example\n", encoding="utf-8")

    with pytest.raises(CuratedCsvError, match="missing columns"):
        load_curated_csv(path)


def test_load_curated_csv_rejects_unknown_enum(tmp_path):
    path = write_csv(tmp_path, "1,Example,,,,,,,,,,disaster,,\n")

    with pytest.raises(CuratedCsvError, match="research_status"):
        load_curated_csv(path)


def test_load_curated_csv_rejects_duplicate_appid(tmp_path):
    path = write_csv(
        tmp_path,
        "1,Example,,,,unknown,,,,,,not_researched,,\n"
        "1,Example Again,,,,unknown,,,,,,not_researched,,\n",
    )

    with pytest.raises(CuratedCsvError, match="already used"):
        load_curated_csv(path)


def test_load_curated_csv_rejects_label_without_research(tmp_path):
    # A label with no research behind it must not enter ground truth.
    path = write_csv(tmp_path, "1,Example,,,,unknown,,,,flop,high,not_researched,,\n")

    with pytest.raises(CuratedCsvError, match="research_status is not_researched"):
        load_curated_csv(path)


def test_load_curated_csv_rejects_research_without_label(tmp_path):
    path = write_csv(tmp_path, "1,Example,,,,unknown,,,,,,researched,,\n")

    with pytest.raises(CuratedCsvError, match="resolved_outcome is empty"):
        load_curated_csv(path)


# --- backfill -------------------------------------------------------------


def windowed_steam_client(window_totals: dict[str, int] | None = None) -> SteamClient:
    """Steam stub whose review totals depend on the requested date window.

    Mirrors the real API: a windowed query aggregates only reviews created
    inside the window, so it returns a smaller total than the lifetime one.
    """
    totals = window_totals or {"lifetime": 909380, "windowed": 19381}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/appdetails"):
            return httpx.Response(200, json=load_fixture("appdetails_released.json"))
        if "/appreviews/" in path:
            params = request.url.params
            payload = load_fixture("appreviews_released.json")
            if "start_date" in params:
                assert params["filter"] == "all"
                assert params["date_range_type"] == "include"
                total = totals["windowed"]
                payload["query_summary"].update(
                    {"total_reviews": total, "total_positive": 13695, "total_negative": 5686}
                )
            else:
                payload["query_summary"]["total_reviews"] = totals["lifetime"]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=load_fixture("currentplayers.json"))

    return SteamClient(httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def steam():
    client = windowed_steam_client()
    yield client
    client._client.close()


def curated(**overrides) -> CuratedRelease:
    base = {
        "steam_appid": RELEASED_APPID,
        "game_name": "Example Released Game",
        "original_release_date": date(2018, 10, 26),
        "launch_price_cents": 5999,
        "resolved_outcome": Outcome.SUCCESS,
        "label_confidence": LabelConfidence.HIGH,
        "research_status": ResearchStatus.RESEARCHED,
        "budget_tier": BudgetTier.AAA,
        "notes": "seed note",
        "sources": "example.invalid",
    }
    base.update(overrides)
    return CuratedRelease(**base)


def test_backfill_creates_release_with_all_windows(session, steam):
    result = backfill_release(session, curated(), steam)
    session.commit()

    assert result.created is True
    assert result.warnings == []
    release = session.scalar(select(HistoricalRelease))
    assert release.game_name == "Example Released Game"
    assert release.steam_release_date == date(2018, 10, 26)
    assert release.cohort_year == 2018
    assert release.metacritic_score == 86
    assert release.resolved_outcome is Outcome.SUCCESS
    assert release.is_trainable is True

    windows = {w.window_key: w for w in session.scalars(select(ReleaseWindow))}
    assert set(windows) == {
        WindowKey.LAUNCH_2W,
        WindowKey.LAUNCH_1M,
        WindowKey.LAUNCH_3M,
        WindowKey.LIFETIME,
    }
    assert windows[WindowKey.LAUNCH_2W].window_start == date(2018, 10, 26)
    assert windows[WindowKey.LAUNCH_2W].window_end == date(2018, 11, 9)
    # Historical CCU is unrecoverable; the column stays null rather than
    # being filled with a current reading that means something else.
    assert windows[WindowKey.LAUNCH_2W].peak_concurrent_players is None


def test_windowed_totals_are_not_lifetime_totals(session, steam):
    backfill_release(session, curated(), steam)
    session.commit()

    windows = {w.window_key: w for w in session.scalars(select(ReleaseWindow))}
    assert windows[WindowKey.LIFETIME].review_total == 909380
    assert windows[WindowKey.LAUNCH_2W].review_total == 19381
    assert windows[WindowKey.LAUNCH_2W].positive_pct == 70.7
    assert windows[WindowKey.LIFETIME].review_total > windows[WindowKey.LAUNCH_2W].review_total


def test_launch_price_comes_from_research_not_the_api(session, steam):
    # The API's `initial` price is today's list price; publishers re-tier old
    # titles, so it must not stand in for the launch price.
    result = backfill_release(session, curated(launch_price_cents=3999), steam)
    session.commit()

    assert result.release.launch_price_cents == 3999
    assert result.release.current_list_price_cents == 5999


def test_platform_launch_type_derived_when_not_curated(session, steam):
    result = backfill_release(session, curated(original_release_date=date(2016, 1, 1)), steam)

    assert result.release.platform_launch_type is PlatformLaunchType.DELAYED_PORT


def test_curated_platform_launch_type_wins(session, steam):
    result = backfill_release(
        session, curated(platform_launch_type=PlatformLaunchType.FORMER_EXCLUSIVE), steam
    )

    assert result.release.platform_launch_type is PlatformLaunchType.FORMER_EXCLUSIVE


def test_rerun_refreshes_api_fields_without_duplicating_windows(session, steam):
    backfill_release(session, curated(), steam)
    session.commit()

    result = backfill_release(session, curated(notes="updated note"), steam)
    session.commit()

    assert result.created is False
    assert len(session.scalars(select(HistoricalRelease)).all()) == 1
    assert len(session.scalars(select(ReleaseWindow)).all()) == 4
    assert result.release.notes == "updated note"


def test_name_mismatch_is_reported_not_silently_accepted(session, steam):
    result = backfill_release(session, curated(game_name="Some Other Game"), steam)

    assert any("name mismatch" in warning for warning in result.warnings)


def test_trademark_symbols_do_not_trigger_a_mismatch(session, steam):
    result = backfill_release(session, curated(game_name="Example Released Game™"), steam)

    assert result.warnings == []


def test_unlabeled_row_is_not_trainable(session, steam):
    result = backfill_release(
        session,
        curated(
            resolved_outcome=None,
            label_confidence=None,
            research_status=ResearchStatus.NOT_RESEARCHED,
        ),
        steam,
    )
    session.commit()

    assert result.release.is_trainable is False
    assert result.release.research_status is ResearchStatus.NOT_RESEARCHED
    # The API half is still populated — only the label is missing.
    assert result.release.steam_release_date == date(2018, 10, 26)


def test_windows_that_have_not_elapsed_are_skipped(session, monkeypatch):
    """A window still in progress must not be stored as if it were complete.

    Steam reports the 1.0 date for a title that left Early Access, so a
    "historical" row can legitimately carry a very recent release date — and
    a 3-month window measured over three weeks returns a real-looking number
    for the wrong period.
    """
    import app.backfill as backfill_module

    recent = load_fixture("appdetails_released.json")
    recent["1174180"]["data"]["release_date"] = {"coming_soon": False, "date": "Oct 1, 2025"}

    class FrozenDatetime(backfill_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return backfill_module.datetime(2025, 10, 20, tzinfo=tz)

    monkeypatch.setattr(backfill_module, "datetime", FrozenDatetime)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/appdetails"):
            return httpx.Response(200, json=recent)
        return httpx.Response(200, json=load_fixture("appreviews_released.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = backfill_release(session, curated(), SteamClient(http_client))
        session.commit()

    written = set(result.windows_written)
    assert WindowKey.LAUNCH_2W in written  # 14 days: elapsed
    assert WindowKey.LAUNCH_1M not in written  # 30 days: not yet
    assert WindowKey.LAUNCH_3M not in written
    assert any("launch_1m has not elapsed" in w for w in result.warnings)
    stored = {w.window_key for w in session.scalars(select(ReleaseWindow))}
    assert WindowKey.LAUNCH_3M not in stored


def test_future_release_date_skips_all_launch_windows(session):
    future = load_fixture("appdetails_released.json")
    future["1174180"]["data"]["release_date"] = {"coming_soon": False, "date": "Jan 1, 2030"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/appdetails"):
            return httpx.Response(200, json=future)
        return httpx.Response(200, json=load_fixture("appreviews_released.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = backfill_release(session, curated(), SteamClient(http_client))

    assert result.windows_written == [WindowKey.LIFETIME]
    assert any("in the future" in w for w in result.warnings)
