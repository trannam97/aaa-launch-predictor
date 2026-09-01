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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
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


class ItadError(RuntimeError):
    """ITAD could not answer."""


class ItadAuthError(ItadError):
    """The key was missing, rejected, or the request never reached ITAD.

    Separate from ItadError because it is never per-row: whatever is wrong will
    be wrong for every remaining game, so the caller should stop rather than
    repeat the same failure once per title.
    """


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
        """False when the earliest record is too late to speak for the launch."""
        if self.suspect_shape or self.days_after_release is None:
            return False
        return -1 <= self.days_after_release <= LAUNCH_WINDOW_DAYS

    @property
    def note(self) -> str:
        if self.suspect_shape:
            # Reported before the date arithmetic, because a partly-understood
            # payload makes "how old is the oldest record" meaningless.
            return f"UNRELIABLE — {self.coverage}, so this may not be the oldest"
        if self.days_after_release is None:
            return "no release date to measure against"
        if self.is_launch_price:
            return f"observed {self.days_after_release}d after release"
        if self.days_after_release < 0:
            return f"observed {-self.days_after_release}d BEFORE release — pre-order listing"
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

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        # A secret pasted with a trailing newline reaches the API as %0A and is
        # rejected as invalid, which looks nothing like the cause.
        self._key = api_key.strip()
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

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = self._client.get(f"{ITAD_BASE}{path}", params={**params, "key": self._key})
        except httpx.HTTPError as exc:
            raise ItadError(f"{path}: {exc}") from exc
        if response.status_code == 403:
            raise ItadAuthError(f"{path}: {_explain_403(response.text)}")
        if response.status_code >= 400:
            raise ItadError(f"{path}: HTTP {response.status_code} {response.text[:120]}")
        return response.json()

    def lookup(self, steam_appid: int) -> str | None:
        """Resolve a Steam appid to an ITAD game id, or None if unknown to ITAD."""
        payload = self._get("/games/lookup/v1", {"appid": steam_appid})
        if not isinstance(payload, dict) or not payload.get("found"):
            return None
        game = payload.get("game")
        if isinstance(game, dict) and isinstance(game.get("id"), str):
            return game["id"]
        return None

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
