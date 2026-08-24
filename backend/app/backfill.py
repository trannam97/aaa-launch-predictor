"""Phase 0.5: build `historical_releases` rows from Steam plus curated research.

Two halves meet here. Steam answers the structural and windowed-review
fields for any appid, including delisted titles. Everything Steam cannot
know — the true first-release date, budget tier, what happened to the studio,
and the outcome label itself — comes from the curated CSV.

The split is deliberate: re-running a backfill refreshes the API half without
ever overwriting research.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BudgetTier,
    DemoTiming,
    HistoricalRelease,
    LabelConfidence,
    Outcome,
    PlatformLaunchType,
    ReleaseWindow,
    ResearchStatus,
    StudioSignal,
    SupportSignal,
    WindowKey,
    utcnow,
)
from app.steam import AppDetails, SteamClient

# Windows captured for every backfilled game, as days from the Steam release.
WINDOW_DAYS: dict[WindowKey, int] = {
    WindowKey.LAUNCH_2W: 14,
    WindowKey.LAUNCH_1M: 30,
    WindowKey.LAUNCH_3M: 90,
}

# A Steam release within this many days of the original counts as day-one.
# Wide enough to absorb timezone and staggered-regional-rollout noise.
DAY_ONE_TOLERANCE_DAYS = 7

# A demo dated within this many days either side of release is treated as a
# launch-window demo rather than a genuine pre-launch one — same-day demos are
# ambiguous, and a pre-launch feature must not lean on an ambiguous case.
DEMO_LAUNCH_WINDOW_DAYS = 3

# DLC dated within this many days of release counts as launch-day content —
# season passes, deluxe-edition items, pre-order bonuses.
DLC_LAUNCH_WINDOW_DAYS = 3

# Dating every DLC costs one request each, and a long-running live-service
# title can have dozens. Capped so one outlier cannot dominate a backfill —
# requests are throttled at ~1.5s, so twenty lookups is half a minute on a
# single game.
MAX_DLC_LOOKUPS = 12


@dataclass(slots=True)
class CuratedRelease:
    """One row of researched input — everything Steam cannot tell us."""

    steam_appid: int
    game_name: str
    original_release_date: date | None = None
    platform_launch_type: PlatformLaunchType | None = None
    platform_reach: str | None = None
    budget_tier: BudgetTier = BudgetTier.UNKNOWN
    launch_price_cents: int | None = None
    post_launch_support: str | None = None
    studio_outcome: str | None = None
    studio_signal: StudioSignal = StudioSignal.UNKNOWN
    support_signal: SupportSignal = SupportSignal.UNKNOWN
    resolved_outcome: Outcome | None = None
    label_confidence: LabelConfidence | None = None
    research_status: ResearchStatus = ResearchStatus.NOT_RESEARCHED
    notes: str | None = None
    sources: str | None = None


@dataclass(slots=True)
class BackfillResult:
    release: HistoricalRelease
    created: bool
    windows_written: list[WindowKey] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def derive_platform_launch_type(
    steam_release: date | None, original_release: date | None
) -> PlatformLaunchType:
    """Classify a Steam launch from the gap to the title's true first release.

    Only two of the three cases are derivable from dates: whether the Steam
    release was day-one or delayed. Telling a plain delayed port apart from a
    former platform exclusive needs knowledge of the exclusivity deal, so
    that value has to come from the curated CSV.
    """
    if steam_release is None or original_release is None:
        return PlatformLaunchType.UNKNOWN
    gap_days = (steam_release - original_release).days
    if abs(gap_days) <= DAY_ONE_TOLERANCE_DAYS:
        return PlatformLaunchType.DAY_ONE_STEAM
    if gap_days > DAY_ONE_TOLERANCE_DAYS:
        return PlatformLaunchType.DELAYED_PORT
    # Steam earlier than the recorded "original" means the curated date is
    # wrong; say so rather than inventing a classification.
    return PlatformLaunchType.UNKNOWN


@dataclass(slots=True)
class DlcSummary:
    launch_day: int | None = None
    post_launch: int | None = None
    last_gap_days: int | None = None


def _summarize_dlc(details: AppDetails, client: SteamClient, warnings: list[str]) -> DlcSummary:
    """Split DLC into launch-day and post-launch by dating each one."""
    if not details.dlc_appids:
        return DlcSummary(launch_day=0, post_launch=0)
    if details.release_date is None:
        return DlcSummary()

    lookups = details.dlc_appids[:MAX_DLC_LOOKUPS]
    if len(details.dlc_appids) > MAX_DLC_LOOKUPS:
        warnings.append(
            f"{len(details.dlc_appids)} DLC listed; dated only the first {MAX_DLC_LOOKUPS}"
        )

    launch_day = post_launch = 0
    last_gap: int | None = None
    for dlc_appid in lookups:
        dlc_release = client.get_release_date_of(dlc_appid)
        if dlc_release is None:
            continue
        gap = (dlc_release - details.release_date).days
        if abs(gap) <= DLC_LAUNCH_WINDOW_DAYS:
            launch_day += 1
        elif gap > DLC_LAUNCH_WINDOW_DAYS:
            post_launch += 1
            last_gap = gap if last_gap is None else max(last_gap, gap)
    return DlcSummary(launch_day=launch_day, post_launch=post_launch, last_gap_days=last_gap)


def classify_demo_timing(
    game_release: date | None, demo_release: date | None, has_demo: bool
) -> DemoTiming:
    """Place a demo relative to the game's Steam release.

    Only `PRE_LAUNCH` is safe to use as a pre-launch feature. A post-launch
    demo is a response to how the launch went, so feeding it to a forecaster
    leaks the outcome backwards into the prediction.
    """
    if not has_demo:
        # No demo listed today. Not the same as none ever existing — Next Fest
        # demos are routinely delisted after the event.
        return DemoTiming.NONE_LISTED
    if game_release is None or demo_release is None:
        return DemoTiming.UNKNOWN
    gap_days = (demo_release - game_release).days
    if gap_days < -DEMO_LAUNCH_WINDOW_DAYS:
        return DemoTiming.PRE_LAUNCH
    if gap_days > DEMO_LAUNCH_WINDOW_DAYS:
        return DemoTiming.POST_LAUNCH
    return DemoTiming.LAUNCH_WINDOW


def _as_utc_datetime(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def get_release_by_appid(session: Session, appid: int) -> HistoricalRelease | None:
    return session.scalar(select(HistoricalRelease).where(HistoricalRelease.steam_appid == appid))


def backfill_release(
    session: Session, curated: CuratedRelease, client: SteamClient
) -> BackfillResult:
    """Fetch one game's Steam data and merge it with its curated research.

    The caller owns the transaction — this flushes but does not commit.
    """
    details = client.get_app_details(curated.steam_appid)
    warnings: list[str] = []

    # One extra request, and only for the minority of titles that list a demo.
    demo_appid = details.demo_appids[0] if details.demo_appids else None
    demo_release = client.get_demo_release_date(demo_appid) if demo_appid else None
    dlc = _summarize_dlc(details, client, warnings)

    if not _names_match(details.name, curated.game_name):
        warnings.append(
            f"name mismatch: CSV says {curated.game_name!r}, Steam says {details.name!r}"
        )

    release = get_release_by_appid(session, curated.steam_appid)
    created = release is None
    if release is None:
        release = HistoricalRelease(steam_appid=curated.steam_appid)
        session.add(release)

    _apply_api_fields(release, details)
    release.demo_appid = demo_appid
    release.demo_release_date = demo_release
    release.demo_timing = classify_demo_timing(
        details.release_date, demo_release, has_demo=demo_appid is not None
    )
    release.dlc_count = len(details.dlc_appids)
    release.has_in_app_purchases = details.has_in_app_purchases
    release.launch_day_dlc_count = dlc.launch_day
    release.post_launch_dlc_count = dlc.post_launch
    release.last_dlc_days_after_launch = dlc.last_gap_days
    _apply_curated_fields(release, curated, details)
    release.backfilled_at = utcnow()
    session.flush()

    written = _write_windows(session, release, details, client, warnings)
    return BackfillResult(
        release=release, created=created, windows_written=written, warnings=warnings
    )


def _apply_api_fields(release: HistoricalRelease, details: AppDetails) -> None:
    release.game_name = details.name
    release.developer = "\n".join(details.developers) or None
    release.publisher = "\n".join(details.publishers) or None
    release.genres = "\n".join(details.genres) or None
    release.steam_release_date = details.release_date
    release.current_list_price_cents = details.price_initial_cents
    release.price_currency = details.price_currency
    release.on_windows = details.on_windows
    release.on_mac = details.on_mac
    release.on_linux = details.on_linux
    release.metacritic_score = details.metacritic_score
    release.metacritic_url = details.metacritic_url
    release.cohort_year = details.release_date.year if details.release_date else None


def _apply_curated_fields(
    release: HistoricalRelease, curated: CuratedRelease, details: AppDetails
) -> None:
    release.original_release_date = curated.original_release_date
    release.platform_launch_type = curated.platform_launch_type or derive_platform_launch_type(
        details.release_date, curated.original_release_date
    )
    release.platform_reach = curated.platform_reach
    release.budget_tier = curated.budget_tier
    release.launch_price_cents = curated.launch_price_cents
    release.post_launch_support = curated.post_launch_support
    release.studio_outcome = curated.studio_outcome
    release.studio_signal = curated.studio_signal
    release.support_signal = curated.support_signal
    release.resolved_outcome = curated.resolved_outcome
    release.label_confidence = curated.label_confidence
    release.research_status = curated.research_status
    release.notes = curated.notes
    release.sources = curated.sources


def _write_windows(
    session: Session,
    release: HistoricalRelease,
    details: AppDetails,
    client: SteamClient,
    warnings: list[str],
) -> list[WindowKey]:
    """Replace this release's window rows with freshly fetched figures."""
    existing = {window.window_key: window for window in release.windows}
    written: list[WindowKey] = []

    lifetime = client.get_review_summary(release.steam_appid)
    _upsert_window(session, release, existing, WindowKey.LIFETIME, None, None, lifetime)
    written.append(WindowKey.LIFETIME)

    steam_release = details.release_date
    if steam_release is None:
        warnings.append("no Steam release date; launch windows skipped")
        return written

    today = datetime.now(UTC).date()
    if steam_release > today:
        warnings.append(
            f"Steam release date {steam_release} is in the future; launch windows skipped"
        )
        return written

    for key, days in WINDOW_DAYS.items():
        end = steam_release + timedelta(days=days)
        if end > today:
            # A window that has not finished elapsing would return a real
            # number for a shorter period than it claims to cover — worse
            # than no number, because nothing downstream would notice.
            warnings.append(f"{key.value} has not elapsed yet (ends {end}); skipped")
            _drop_window(session, existing, key)
            continue
        summary = client.get_review_summary(
            release.steam_appid, _as_utc_datetime(steam_release), _as_utc_datetime(end)
        )
        _upsert_window(session, release, existing, key, steam_release, end, summary)
        written.append(key)

    return written


def _drop_window(session: Session, existing: dict, key: WindowKey) -> None:
    """Remove a previously-written window that is no longer valid."""
    window = existing.pop(key, None)
    if window is not None:
        session.delete(window)


def _upsert_window(
    session: Session,
    release: HistoricalRelease,
    existing: dict,
    key: WindowKey,
    start: date | None,
    end: date | None,
    summary,
) -> None:
    window = existing.get(key)
    if window is None:
        window = ReleaseWindow(release=release, window_key=key)
        session.add(window)
    window.window_start = start
    window.window_end = end
    window.review_total = summary.total
    window.review_positive = summary.positive
    window.review_negative = summary.negative
    window.review_score_desc = summary.score_desc
    # peak_concurrent_players stays null for backfilled games: Steam exposes
    # only a live player count, so historical CCU is genuinely unrecoverable.


def _names_match(steam_name: str, curated_name: str) -> bool:
    """Loose comparison — Steam names carry ™/® and stylized punctuation."""
    return _normalize(steam_name) == _normalize(curated_name)


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


CSV_COLUMNS = (
    "steam_appid",
    "game_name",
    "original_release_date",
    "platform_launch_type",
    "platform_reach",
    "budget_tier",
    "launch_price_usd",
    "post_launch_support",
    "studio_outcome",
    "studio_signal",
    "support_signal",
    "resolved_outcome",
    "label_confidence",
    "research_status",
    "notes",
    "sources",
)


class CuratedCsvError(ValueError):
    """A curated CSV row could not be parsed."""


def load_curated_csv(path) -> list[CuratedRelease]:
    """Parse the curated research CSV into `CuratedRelease` rows.

    Strict on purpose: a typo'd enum or appid fails the whole load with the
    offending line number, rather than silently importing a row with a
    missing label into what is meant to be ground truth.
    """
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise CuratedCsvError(f"{path}: missing columns {sorted(missing)}")

        rows: list[CuratedRelease] = []
        seen: dict[int, int] = {}
        for line_no, raw in enumerate(reader, start=2):
            try:
                row = _parse_row(raw)
            except (ValueError, KeyError) as exc:
                raise CuratedCsvError(f"{path} line {line_no}: {exc}") from exc
            if row.steam_appid in seen:
                raise CuratedCsvError(
                    f"{path} line {line_no}: appid {row.steam_appid} already used "
                    f"on line {seen[row.steam_appid]}"
                )
            seen[row.steam_appid] = line_no
            rows.append(row)
    return rows


def _parse_row(raw: dict) -> CuratedRelease:
    outcome = _enum_or_none(Outcome, _text(raw["resolved_outcome"]), "resolved_outcome")
    status = (
        _enum_or_none(ResearchStatus, _text(raw["research_status"]), "research_status")
        or ResearchStatus.NOT_RESEARCHED
    )
    if outcome is not None and status is ResearchStatus.NOT_RESEARCHED:
        raise ValueError("resolved_outcome is set but research_status is not_researched")
    if outcome is None and status is ResearchStatus.RESEARCHED:
        raise ValueError("research_status is researched but resolved_outcome is empty")

    price = _text(raw["launch_price_usd"])
    return CuratedRelease(
        steam_appid=int(_required(raw["steam_appid"], "steam_appid")),
        game_name=_required(raw["game_name"], "game_name"),
        original_release_date=_date_or_none(_text(raw["original_release_date"])),
        platform_launch_type=_enum_or_none(
            PlatformLaunchType, _text(raw["platform_launch_type"]), "platform_launch_type"
        ),
        platform_reach=_text(raw["platform_reach"]),
        budget_tier=_enum_or_none(BudgetTier, _text(raw["budget_tier"]), "budget_tier")
        or BudgetTier.UNKNOWN,
        launch_price_cents=round(float(price) * 100) if price else None,
        post_launch_support=_text(raw["post_launch_support"]),
        studio_outcome=_text(raw["studio_outcome"]),
        studio_signal=_enum_or_none(StudioSignal, _text(raw["studio_signal"]), "studio_signal")
        or StudioSignal.UNKNOWN,
        support_signal=_enum_or_none(SupportSignal, _text(raw["support_signal"]), "support_signal")
        or SupportSignal.UNKNOWN,
        resolved_outcome=outcome,
        label_confidence=_enum_or_none(
            LabelConfidence, _text(raw["label_confidence"]), "label_confidence"
        ),
        research_status=status,
        notes=_text(raw["notes"]),
        sources=_text(raw["sources"]),
    )


def _text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _required(value: str | None, column: str) -> str:
    cleaned = _text(value)
    if cleaned is None:
        raise ValueError(f"{column} is required")
    return cleaned


def _date_or_none(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _enum_or_none(enum_cls, value: str | None, column: str):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ValueError(f"{column}={value!r} is not one of: {allowed}") from exc
