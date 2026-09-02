"""Launch prices from IsThereAnyDeal's price history.

`launch_price_cents` is curated because Steam cannot answer it: `price_overview.
initial` is *today's* list price, and The Witcher 3 launched at $59.99 while
listing at $39.99. Only 36 of 206 rows carry one, and a missing price does not
read as missing — `app/features.py` turns it into `0.0`, which the model reads
as a free game.

ITAD is the right source rather than asking a model: a launch price is a fact
with a database behind it, the same reason release dates come from Wikidata.

Three constraints shape what this returns.

**Standard edition only, structurally.** The join is on Steam appid, and a
deluxe or gold tier is a *separate Steam app* — the same fact that makes
`dlc_count` count only separate apps. Joining on the base appid therefore
cannot pick up an edition upgrade; nothing needs to filter for it.

**US only.** `country=US`, matching what `launch_price_usd` has always meant.

**The earliest price ITAD holds is not always the launch price.** ITAD began
tracking each title at some point, and for a 2015 release that point may be
years late. Every result carries how far after release the observation sits and
whether that is close enough to call it a launch price, so a stale reading is
visible rather than silently curated in.

## What a 403 means here

Three different faults arrive as HTTP 403 and are not interchangeable, which is
why the response body is reported rather than summarised:

- ``{"reason_phrase": "Missing api key"}`` — the key never reached the API. The
  key goes in the ``key`` query parameter; ``Authorization: Bearer`` and
  ``X-API-Key`` are both ignored and read as missing.
- ``{"reason_phrase": "Invalid or expired api key"}`` — it arrived and was
  rejected. A trailing newline from a pasted secret lands here, which is why the
  key is stripped.
- ``error code: 1010`` in a non-JSON body — Cloudflare, not ITAD, refusing the
  request outright. Sending no ``User-Agent`` at all triggers it.

## The budget is small, and it is per app

The first run over the whole queue got 50 games through and then took HTTP 429
for each of the remaining 120 in twenty seconds flat — 100 requests spent, and
no recovery inside the run. Three things follow, and all three are here:

- **Ask for less.** Resolving appids one at a time cost a keyed request per
  game, doubling the traffic for no information. ``lookup_many`` resolves the
  entire queue in a single keyless POST, so only the history calls are left.
- **Space them out.** ``MIN_REQUEST_INTERVAL_SECONDS`` throttles, the same way
  ``app/steam.py`` does.
- **Stop when the budget is gone.** ``ItadRateLimitError`` is separate from
  ``ItadError`` because a 429 is never per-row either: burning through 120
  games to collect 120 identical warnings is what the first run did.

ITAD sends ``Retry-After`` with a 429 and rate-limits in tiers, higher for a
verified account. The exact allowance is not a constant to hardcode — it is
shown on your app's page at isthereanydeal.com/apps/dev, and the first 429 of a
run logs what ITAD asked us to wait.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx

ITAD_BASE = "https://api.isthereanydeal.com"

# Verified against GET /service/shops/v1?country=US, which needs no API key.
STEAM_SHOP_ID = 61
LAUNCH_REGION = "US"

# How close to release an observation must sit to be called the launch price.
# Publishers re-tier on a scale of years, not weeks, and the *regular* price is
# read rather than the deal price, so a launch discount does not disturb this.
LAUNCH_WINDOW_DAYS = 60

# How far before release to start asking. A premium tier can list days ahead of
# the standard edition, and the request should not cut that off.
SINCE_MARGIN_DAYS = 30

TIMEOUT_SECONDS = 20.0

# Space requests out. The unthrottled first run spent the whole allowance in
# eighteen seconds; at this interval the same 170 games take about three
# minutes, which is nothing next to a run that returns 120 failures.
MIN_REQUEST_INTERVAL_SECONDS = 1.0

# What to do when the allowance runs out anyway. ITAD sends `Retry-After`; when
# it does not, back off geometrically from this.
RATE_LIMIT_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_SECONDS = 20.0

# Waiting longer than this is not a retry, it is a stalled CI job. A budget that
# needs more than a two-minute pause is a budget this run cannot finish, and the
# job resumes from its proposals file rather than sitting on a paid runner.
MAX_RETRY_WAIT_SECONDS = 120.0

# How many appids to resolve per bulk lookup. The whole 206-row corpus goes in
# one request today, verified against the live API; the batch exists so a corpus
# grown past 300 does not discover an undocumented ceiling as a blanket failure.
LOOKUP_BATCH_SIZE = 200


class ItadError(RuntimeError):
    """ITAD could not answer."""


class ItadAuthError(ItadError):
    """The key was missing, rejected, or the request never reached ITAD.

    Separate from ItadError because it is never per-row: whatever is wrong will
    be wrong for every remaining game, so the caller should stop rather than
    repeat the same failure once per title.
    """


class ItadRateLimitError(ItadError):
    """The request allowance is spent.

    Separate from ItadError for the same reason ItadAuthError is: it is never
    per-row. The run that produced this class asked 120 more times after the
    budget was gone and logged 120 identical warnings, when the true answer was
    "stop, keep what you have, come back later".
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ItadShapeError(ItadError):
    """The response parsed as JSON but not as anything recognisable.

    Raised with the keys actually seen, because the fix is to teach `_observe`
    one more spelling and the error should carry what to teach it.
    """


@dataclass(slots=True)
class HistoryRead:
    """What a history payload yielded, and how much of it was understood.

    The coverage counts exist because silent partial parsing is the failure
    this parser is most likely to have: `_observe` returns None for any shape
    it does not recognise, so understanding one entry out of three hundred
    looks identical to a game with a single price record. It is not — and the
    difference decides whether a result means anything.
    """

    observation: PriceObservation | None
    parsed: int
    total: int
    oldest: date | None = None
    newest: date | None = None

    @property
    def coverage(self) -> str:
        span = ""
        if self.oldest and self.newest:
            span = f", spanning {self.oldest} to {self.newest}"
        return f"parsed {self.parsed} of {self.total} history entries{span}"

    @property
    def is_suspect(self) -> bool:
        """Too little understood for the oldest record to be meaningful."""
        return self.total > 1 and self.parsed < self.total


@dataclass(slots=True)
class PriceObservation:
    """One recorded regular price. Not a deal price — see `_observe`."""

    price_cents: int
    recorded_on: date


@dataclass(slots=True)
class LaunchPrice:
    """What ITAD knows, and how much of a launch price it actually is."""

    price_cents: int
    observed_on: date
    days_after_release: int | None
    coverage: str = ""
    suspect_shape: bool = False

    @property
    def is_launch_price(self) -> bool:
        """Whether this record can speak for the launch.

        The window opens before release on purpose. `history` is asked from
        SINCE_MARGIN_DAYS ahead of the date, so the oldest record ITAD holds is
        typically the pre-order listing — and a pre-order listing's *regular*
        price is the launch price. Publishers set it once and charge it on the
        day; a launch discount appears as a `cut` against that same regular
        figure, which `_observe` already reads past.

        An earlier bound of -1 rejected exactly the records the margin exists
        to fetch: every row in the first working run sat at -25 to -30 days and
        was thrown away as "too late".
        """
        if self.suspect_shape or self.days_after_release is None:
            return False
        return -SINCE_MARGIN_DAYS <= self.days_after_release <= LAUNCH_WINDOW_DAYS

    @property
    def note(self) -> str:
        if self.suspect_shape:
            # Reported before the date arithmetic, because a partly-understood
            # payload makes "how old is the oldest record" meaningless.
            return f"UNRELIABLE — {self.coverage}, so this may not be the oldest"
        if self.days_after_release is None:
            return "no release date to measure against"
        if self.is_launch_price:
            if self.days_after_release < 0:
                return f"pre-order listing, {-self.days_after_release}d before release"
            return f"observed {self.days_after_release}d after release"
        if self.days_after_release < 0:
            return (
                f"observed {-self.days_after_release}d before release — earlier than the "
                "window asked for, so not necessarily the launch listing"
            )
        return (
            f"earliest record is {self.days_after_release}d after release, "
            f"not the launch price ({self.coverage})"
        )


def _amount_to_cents(value: Any) -> int | None:
    """ITAD reports money as a decimal amount; the corpus stores integer cents."""
    if isinstance(value, dict):
        value = value.get("amount", value.get("amountInt"))
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def _parse_when(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _observe(entry: Any) -> PriceObservation | None:
    """Read one history entry, preferring the regular price over the deal price.

    Every ITAD record carries the undiscounted regular price alongside whatever
    was being charged, so a launch-week sale does not have to be filtered out —
    reading `regular` sidesteps it entirely.
    """
    if not isinstance(entry, dict):
        return None
    body = entry.get("deal") if isinstance(entry.get("deal"), dict) else entry

    cents = None
    for key in ("regular", "regular_price", "price_regular"):
        cents = _amount_to_cents(body.get(key))
        if cents is not None:
            break
    if cents is None and not body.get("cut"):
        # No regular field, but this record is undiscounted, so its price is it.
        for key in ("price", "price_new", "amount"):
            cents = _amount_to_cents(body.get(key))
            if cents is not None:
                break
    if cents is None:
        return None

    when = None
    for key in ("timestamp", "time", "date", "recorded"):
        when = _parse_when(entry.get(key) or body.get(key))
        if when is not None:
            break
    if when is None:
        return None
    return PriceObservation(price_cents=cents, recorded_on=when)


def earliest_regular_price(history: Any) -> HistoryRead:
    """The oldest regular price in a payload, with how much of it was read."""
    entries = history
    if isinstance(history, dict):
        for key in ("history", "data", "prices"):
            if isinstance(history.get(key), list):
                entries = history[key]
                break
    if not isinstance(entries, list):
        raise ItadShapeError(f"expected a list of history entries, got {type(entries).__name__}")
    if not entries:
        return HistoryRead(observation=None, parsed=0, total=0)

    seen = [obs for obs in (_observe(e) for e in entries) if obs is not None]
    if not seen:
        keys = sorted(entries[0].keys()) if isinstance(entries[0], dict) else repr(entries[0])[:80]
        raise ItadShapeError(f"no price/date pair in a history entry; keys seen: {keys}")
    dates = [obs.recorded_on for obs in seen]
    return HistoryRead(
        observation=min(seen, key=lambda obs: obs.recorded_on),
        parsed=len(seen),
        total=len(entries),
        oldest=min(dates),
        newest=max(dates),
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After` as seconds. It is legal as either a count or an HTTP date."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = datetime.now(when.tzinfo)
    return max(0.0, (when - now).total_seconds())


def _describe_wait(seconds: float | None) -> str:
    """What ITAD asked us to wait, or that it declined to say."""
    if seconds is None:
        return "no Retry-After header"
    if seconds >= 90:
        return f"Retry-After {seconds / 60:.0f} min"
    return f"Retry-After {seconds:.0f}s"


def _explain_403(body: str) -> str:
    """Turn ITAD's 403 body into something that says what to fix."""
    text = (body or "").strip()
    if "1010" in text:
        return (
            "blocked by Cloudflare before reaching ITAD (error 1010) — the "
            "request needs a User-Agent header"
        )
    if "Missing api key" in text:
        return (
            "ITAD saw no api key. It must go in the `key` query parameter; "
            "Authorization and X-API-Key headers are ignored"
        )
    if "Invalid or expired" in text:
        return (
            "ITAD rejected the key as invalid or expired. Check the secret for "
            "a stray newline or space, and that it is still active"
        )
    return f"rejected: {text[:120]}"


class ItadClient:
    """Thin ITAD client. Pass a client in tests to stub the network."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        # A secret pasted with a trailing newline reaches the API as %0A and is
        # rejected as invalid, which looks nothing like the cause.
        self._key = api_key.strip()
        self._min_interval = min_request_interval
        self._last_request_at: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "aaa-launch-predictor/0.1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _throttle(self) -> None:
        """Space requests out so a burst cannot spend the whole allowance."""
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """One GET, retried only for 429 and only while the wait is short.

        A 429 is retried rather than raised straight away because the allowance
        may be a short window that a pause outlasts. It is *not* retried
        indefinitely: past MAX_RETRY_WAIT_SECONDS the honest answer is that this
        run cannot finish, and ItadRateLimitError says so once.
        """
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            self._throttle()
            try:
                response = self._client.get(
                    f"{ITAD_BASE}{path}", params={**params, "key": self._key}
                )
            except httpx.HTTPError as exc:
                raise ItadError(f"{path}: {exc}") from exc

            if response.status_code == 429:
                asked = _retry_after_seconds(response)
                wait = asked if asked is not None else RATE_LIMIT_BACKOFF_SECONDS * attempt
                if attempt == RATE_LIMIT_ATTEMPTS or wait > MAX_RETRY_WAIT_SECONDS:
                    raise ItadRateLimitError(
                        f"{path}: HTTP 429, the request allowance is spent "
                        f"({_describe_wait(asked)}). Re-run to continue from the "
                        "proposals already written; isthereanydeal.com/apps/dev "
                        "shows this app's limit and recent usage.",
                        retry_after=asked,
                    )
                time.sleep(wait)
                continue

            if response.status_code == 403:
                raise ItadAuthError(f"{path}: {_explain_403(response.text)}")
            if response.status_code >= 400:
                raise ItadError(f"{path}: HTTP {response.status_code} {response.text[:120]}")
            return response.json()
        raise AssertionError("unreachable: the loop returns or raises")

    def lookup_many(self, steam_appids: list[int]) -> dict[int, str]:
        """Resolve every appid to an ITAD game id in one request.

        `/games/lookup/v1` answers for a single appid and costs a keyed request
        to do it — which is how a 170-game queue turned into 340 requests and
        spent the allowance halfway through. This endpoint takes the whole list
        at once and needs no key at all: the full 206-row corpus resolves in a
        single POST, verified against the live API.

        No key is sent for exactly that reason. It is not needed, and a request
        that does not carry one cannot spend a budget that is counted per app.

        Appids ITAD does not know are simply absent from the result, which is
        what `not tracked` means downstream.
        """
        by_shop_id = {f"app/{appid}": appid for appid in steam_appids}
        keys = sorted(by_shop_id)
        found: dict[int, str] = {}
        for start in range(0, len(keys), LOOKUP_BATCH_SIZE):
            payload = self._lookup_batch(keys[start : start + LOOKUP_BATCH_SIZE])
            found.update(
                {
                    by_shop_id[key]: value
                    for key, value in payload.items()
                    if key in by_shop_id and isinstance(value, str) and value
                }
            )
        return found

    def _lookup_batch(self, shop_ids: list[str]) -> dict[str, Any]:
        path = f"/lookup/id/shop/{STEAM_SHOP_ID}/v1"
        self._throttle()
        try:
            response = self._client.post(f"{ITAD_BASE}{path}", json=shop_ids)
        except httpx.HTTPError as exc:
            raise ItadError(f"{path}: {exc}") from exc
        if response.status_code == 429:
            raise ItadRateLimitError(
                f"{path}: HTTP 429 before a single game was looked up "
                f"({_describe_wait(_retry_after_seconds(response))})"
            )
        if response.status_code >= 400:
            raise ItadError(f"{path}: HTTP {response.status_code} {response.text[:120]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ItadShapeError(f"expected a shop-id to game-id map, got {type(payload).__name__}")
        return payload

    def lookup(self, steam_appid: int) -> str | None:
        """Resolve one Steam appid, via the same keyless bulk endpoint."""
        return self.lookup_many([steam_appid]).get(steam_appid)

    def history(self, itad_id: str, since: date | None = None) -> Any:
        """Raw Steam-only, US price history. Returned unparsed for --dump-raw.

        Without `since` the endpoint answers with a recent window only — a
        captured response held five change events spanning eleven weeks, whose
        regular price was the title's present re-tier rather than anything it
        launched at. `since` is what reaches back past that.
        """
        params: dict[str, Any] = {
            "id": itad_id,
            "shops": STEAM_SHOP_ID,
            "country": LAUNCH_REGION,
        }
        if since is not None:
            params["since"] = f"{since.isoformat()}T00:00:00Z"
        return self._get("/games/history/v2", params)

    def launch_price(self, steam_appid: int, release_date: date | None) -> LaunchPrice | None:
        """Earliest US Steam regular price, with how far from launch it sits."""
        itad_id = self.lookup(steam_appid)
        if itad_id is None:
            return None
        return self.launch_price_for(itad_id, release_date)

    def launch_price_for(self, itad_id: str, release_date: date | None) -> LaunchPrice | None:
        """The same, for an id already resolved by `lookup_many`. One request."""
        since = release_date - timedelta(days=SINCE_MARGIN_DAYS) if release_date else None
        read = earliest_regular_price(self.history(itad_id, since=since))
        if read.observation is None:
            return None
        oldest = read.observation
        gap = (oldest.recorded_on - release_date).days if release_date else None
        return LaunchPrice(
            price_cents=oldest.price_cents,
            observed_on=oldest.recorded_on,
            days_after_release=gap,
            coverage=read.coverage,
            suspect_shape=read.is_suspect,
        )
