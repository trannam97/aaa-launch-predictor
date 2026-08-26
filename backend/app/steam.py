"""Thin client over the public Steam endpoints used in Phase 0.

All three endpoints are public and unkeyed. Steam applies a soft rate limit
(roughly 200 store requests per 5 minutes per IP); the scheduled jobs in
/jobs are what have to respect it, so this client stays simple and does one
request per call.

Nothing here scrapes SteamDB — see the Data Layer section of PROJECT_SPEC.md.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Self

import httpx

from app.config import get_settings

STORE_BASE_URL = "https://store.steampowered.com"
API_BASE_URL = "https://api.steampowered.com"

# Steam tolerates roughly 200 store requests per 5 minutes per IP — about one
# every 1.5s. Pacing lives here rather than in each caller because callers
# cannot see the whole request stream: dating a title's DLC can fire twenty
# requests in a burst on top of the four every game already costs, which is
# exactly how this limit got hit in practice.
MIN_REQUEST_INTERVAL_SECONDS = 1.5

# Steam's own English date strings, in the order we try them. The store API
# switches between the middle-endian and little-endian forms depending on the
# country code, and unreleased titles use coarse windows ("Q4 2026", "2026").
_DATE_FORMATS = ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B %Y", "%b %Y", "%B %Y", "%Y")

# Release-date strings that name a window rather than a day. Narrowing one of
# these ("Q4 2026" -> "Nov 12, 2026") is a gain in precision, not a delay.
COARSE_DATE_PATTERN = re.compile(
    r"^(Q[1-4]\s*\d{4}|\d{4}|[A-Za-z]+\s+\d{4}|coming\s+soon|to\s+be\s+announced|TBA|TBD)$",
    re.IGNORECASE,
)


class SteamError(RuntimeError):
    """Base class for Steam client failures."""


class SteamAppNotFound(SteamError):
    """Steam responded, but has no store data for this appid."""


class SteamUnavailable(SteamError):
    """Steam is rate-limiting us or is otherwise failing."""


@dataclass(slots=True)
class AppDetails:
    """The structural metadata for one app, as returned by the store API."""

    appid: int
    name: str
    app_type: str | None = None
    short_description: str | None = None
    header_image: str | None = None
    developers: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    release_date: date | None = None
    release_date_raw: str | None = None
    coming_soon: bool = False
    is_free: bool = False
    price_initial_cents: int | None = None
    price_final_cents: int | None = None
    discount_percent: int | None = None
    price_currency: str | None = None
    on_windows: bool = False
    on_mac: bool = False
    on_linux: bool = False
    metacritic_score: int | None = None
    metacritic_url: str | None = None
    # Demo apps Steam currently lists for this title. Current state only —
    # a demo taken down after Steam Next Fest leaves no trace here.
    demo_appids: list[int] = field(default_factory=list)
    # DLC sold as separate Steam apps. Note what this does NOT count:
    # content bought with in-game currency (Helldivers 2's Warbonds, battle
    # passes) is invisible here, so a zero means "nothing sold as a Steam
    # app", never "no add-on content". `has_in_app_purchases` is the
    # complement that catches those models.
    dlc_appids: list[int] = field(default_factory=list)
    has_in_app_purchases: bool = False


@dataclass(slots=True)
class ReviewSummary:
    """Aggregate Steam user-review counts. Never blended with critic scores."""

    total: int | None = None
    positive: int | None = None
    negative: int | None = None
    score_desc: str | None = None


def parse_release_date(raw: str | None) -> date | None:
    """Best-effort parse of Steam's release-date string.

    Coarse windows are pinned to the first day they could mean ("Q4 2026" ->
    2026-10-01), which keeps date arithmetic working; the unparsed string is
    stored alongside so the imprecision stays visible.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace(" ", " ")
    if not cleaned:
        return None

    quarter_prefixes = {"q1": 1, "q2": 4, "q3": 7, "q4": 10}
    lowered = cleaned.lower()
    for prefix, month in quarter_prefixes.items():
        if lowered.startswith(prefix):
            year_part = lowered[len(prefix) :].strip()
            if year_part.isdigit() and len(year_part) == 4:
                return date(int(year_part), month, 1)

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


class SteamClient:
    """Synchronous Steam client. Pass a client in tests to stub the network."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        settings = get_settings()
        self._country_code = settings.steam_country_code
        self._language = settings.steam_language
        self._min_interval = min_request_interval
        self._last_request_at: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.steam_timeout_seconds,
            headers={"User-Agent": "aaa-launch-predictor/0.1 (+https://github.com/)"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- requests -----------------------------------------------------

    def _throttle(self) -> None:
        """Space requests out so a burst cannot trip Steam's rate limit."""
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get_json(self, url: str, params: dict) -> dict:
        self._throttle()
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise SteamUnavailable(f"request to {url} failed: {exc}") from exc

        if response.status_code == 429:
            raise SteamUnavailable("Steam rate-limited this client (HTTP 429)")
        if response.status_code >= 500:
            raise SteamUnavailable(f"Steam returned HTTP {response.status_code} for {url}")
        if response.status_code >= 400:
            raise SteamError(f"Steam returned HTTP {response.status_code} for {url}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SteamUnavailable(f"Steam returned non-JSON for {url}") from exc
        if not isinstance(payload, dict):
            raise SteamUnavailable(f"Steam returned an unexpected payload shape for {url}")
        return payload

    def get_app_details(self, appid: int) -> AppDetails:
        """Fetch store metadata for one app.

        Raises SteamAppNotFound when Steam knows the appid but has no store
        page for it (region-locked, delisted, or not a store item).
        """
        payload = self._get_json(
            f"{STORE_BASE_URL}/api/appdetails",
            {"appids": appid, "cc": self._country_code, "l": self._language},
        )
        entry = payload.get(str(appid))
        if not isinstance(entry, dict) or not entry.get("success"):
            raise SteamAppNotFound(f"Steam has no store data for appid {appid}")
        data = entry.get("data")
        if not isinstance(data, dict):
            raise SteamAppNotFound(f"Steam returned an empty record for appid {appid}")
        return _parse_app_details(appid, data)

    def get_review_summary(
        self,
        appid: int,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> ReviewSummary:
        """Fetch aggregate user-review counts (no review bodies).

        With no window, this returns lifetime totals. With one, Steam
        aggregates only reviews created inside it — which is what makes
        launch-window figures recoverable for games released years ago, in a
        single request rather than by paginating every review. Lifetime
        totals must never stand in for launch-window ones: they fold in years
        of later sales and sentiment, erasing the very signal being predicted.
        """
        params: dict[str, object] = {
            "json": 1,
            "language": "all",
            "purchase_type": "all",
            "num_per_page": 0,
            "review_type": "all",
        }
        if (window_start is None) != (window_end is None):
            raise ValueError("window_start and window_end must be given together")
        if window_start is not None and window_end is not None:
            if window_end <= window_start:
                raise ValueError("window_end must be after window_start")
            params.update(
                {
                    # `filter=all` is required for the date range to apply;
                    # Steam ignores it under the default relevance ordering.
                    "filter": "all",
                    "date_range_type": "include",
                    "start_date": int(window_start.timestamp()),
                    "end_date": int(window_end.timestamp()),
                }
            )

        payload = self._get_json(f"{STORE_BASE_URL}/appreviews/{appid}", params)
        if not payload.get("success"):
            return ReviewSummary()
        summary = payload.get("query_summary")
        if not isinstance(summary, dict):
            return ReviewSummary()
        return ReviewSummary(
            total=_as_int(summary.get("total_reviews")),
            positive=_as_int(summary.get("total_positive")),
            negative=_as_int(summary.get("total_negative")),
            score_desc=summary.get("review_score_desc") or None,
        )

    def get_demo_release_date(self, demo_appid: int) -> date | None:
        """When a demo app itself released. See `get_release_date_of`."""
        return self.get_release_date_of(demo_appid)

    def get_release_date_of(self, appid: int) -> date | None:
        """Release date of any app — used for demo and DLC apps.

        The parent game's `demos` and `dlc` lists say only what exists now.
        Dating each child app is what separates content shipped at launch
        from content added afterwards, which are different signals: one is a
        pre-launch monetization decision, the other a response to how the
        launch went.
        """
        payload = self._get_json(
            f"{STORE_BASE_URL}/api/appdetails",
            {"appids": appid, "cc": self._country_code, "l": self._language},
        )
        entry = payload.get(str(appid))
        if not isinstance(entry, dict) or not entry.get("success"):
            return None
        data = entry.get("data")
        if not isinstance(data, dict):
            return None
        release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
        return parse_release_date(release.get("date"))

    def get_current_players(self, appid: int) -> int | None:
        """Current concurrent players, or None if Steam won't report it.

        Unreleased games have no player count; that is a normal None, not an
        error. This is the only CCU source we use — the history we hold is
        whatever we poll and store ourselves, going forward.
        """
        payload = self._get_json(
            f"{API_BASE_URL}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
            {"appid": appid},
        )
        response = payload.get("response")
        if not isinstance(response, dict) or response.get("result") != 1:
            return None
        return _as_int(response.get("player_count"))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _parse_app_details(appid: int, data: dict) -> AppDetails:
    release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
    raw_date = release.get("date") or None

    price = data.get("price_overview") if isinstance(data.get("price_overview"), dict) else {}
    platforms = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
    metacritic = data.get("metacritic") if isinstance(data.get("metacritic"), dict) else {}

    genres = [
        genre["description"]
        for genre in data.get("genres") or []
        if isinstance(genre, dict) and genre.get("description")
    ]

    return AppDetails(
        appid=appid,
        name=data.get("name") or f"Unknown app {appid}",
        app_type=data.get("type") or None,
        short_description=data.get("short_description") or None,
        header_image=data.get("header_image") or None,
        developers=[d for d in data.get("developers") or [] if d],
        publishers=[p for p in data.get("publishers") or [] if p],
        genres=genres,
        release_date=parse_release_date(raw_date),
        release_date_raw=raw_date,
        coming_soon=bool(release.get("coming_soon")),
        is_free=bool(data.get("is_free")),
        price_initial_cents=_as_int(price.get("initial")),
        price_final_cents=_as_int(price.get("final")),
        discount_percent=_as_int(price.get("discount_percent")),
        price_currency=price.get("currency") or None,
        on_windows=bool(platforms.get("windows")),
        on_mac=bool(platforms.get("mac")),
        on_linux=bool(platforms.get("linux")),
        metacritic_score=_as_int(metacritic.get("score")),
        metacritic_url=metacritic.get("url") or None,
        demo_appids=[
            demo_id
            for demo in data.get("demos") or []
            if isinstance(demo, dict) and (demo_id := _as_int(demo.get("appid")))
        ],
        dlc_appids=[dlc_id for raw in data.get("dlc") or [] if (dlc_id := _as_int(raw))],
        has_in_app_purchases=any(
            category.get("description") == "In-App Purchases"
            for category in data.get("categories") or []
            if isinstance(category, dict)
        ),
    )
