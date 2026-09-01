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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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

TIMEOUT_SECONDS = 20.0


class ItadError(RuntimeError):
    """ITAD could not answer."""


class ItadShapeError(ItadError):
    """The response parsed as JSON but not as anything recognisable.

    Raised with the keys actually seen, because the fix is to teach `_observe`
    one more spelling and the error should carry what to teach it.
    """


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

    @property
    def is_launch_price(self) -> bool:
        """False when the earliest record is too late to speak for the launch."""
        if self.days_after_release is None:
            return False
        return -1 <= self.days_after_release <= LAUNCH_WINDOW_DAYS

    @property
    def note(self) -> str:
        if self.days_after_release is None:
            return "no release date to measure against"
        if self.is_launch_price:
            return f"observed {self.days_after_release}d after release"
        if self.days_after_release < 0:
            return f"observed {-self.days_after_release}d BEFORE release — pre-order listing"
        return f"earliest record is {self.days_after_release}d after release, not the launch price"


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


def earliest_regular_price(history: Any) -> PriceObservation | None:
    """The oldest regular price in a history payload, or None if it holds none."""
    entries = history
    if isinstance(history, dict):
        for key in ("history", "data", "prices"):
            if isinstance(history.get(key), list):
                entries = history[key]
                break
    if not isinstance(entries, list):
        raise ItadShapeError(f"expected a list of history entries, got {type(entries).__name__}")
    if not entries:
        return None

    seen = [obs for obs in (_observe(e) for e in entries) if obs is not None]
    if not seen:
        keys = sorted(entries[0].keys()) if isinstance(entries[0], dict) else repr(entries[0])[:80]
        raise ItadShapeError(f"no price/date pair in a history entry; keys seen: {keys}")
    return min(seen, key=lambda obs: obs.recorded_on)


class ItadClient:
    """Thin ITAD client. Pass a client in tests to stub the network."""

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._key = api_key
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
            raise ItadError(f"{path}: rejected — check ITAD_API_KEY")
        if response.status_code >= 400:
            raise ItadError(f"{path}: HTTP {response.status_code}")
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

    def history(self, itad_id: str) -> Any:
        """Raw Steam-only, US price history. Returned unparsed for --dump-raw."""
        return self._get(
            "/games/history/v2",
            {"id": itad_id, "shops": STEAM_SHOP_ID, "country": LAUNCH_REGION},
        )

    def launch_price(self, steam_appid: int, release_date: date | None) -> LaunchPrice | None:
        """Earliest US Steam regular price, with how far from launch it sits."""
        itad_id = self.lookup(steam_appid)
        if itad_id is None:
            return None
        observation = earliest_regular_price(self.history(itad_id))
        if observation is None:
            return None
        gap = (observation.recorded_on - release_date).days if release_date else None
        return LaunchPrice(
            price_cents=observation.price_cents,
            observed_on=observation.recorded_on,
            days_after_release=gap,
        )
