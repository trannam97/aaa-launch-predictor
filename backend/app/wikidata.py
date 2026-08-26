"""Wikidata client, for the one fact Steam cannot tell us: when a game first shipped.

Steam knows its own release date and nothing about the console launch that may
have preceded it by years. Without the original date, `derive_platform_launch_type`
has nothing to compare against and every row falls to `unknown` — which is what
was blocking 166 of the corpus's 205 rows from ever being labeled.

Wikidata carries **P1733, the Steam application ID**, so this is a structured
join rather than scraping: one SPARQL query returns publication dates for a
batch of appids. The earliest date across all platforms is the original release.

Per-platform qualifiers (P400) exist but are inconsistently populated, so they
are deliberately not relied on. The minimum publication date is enough — a 2014
console launch against a 2019 Steam date identifies a delayed port whether or
not either statement names its platform.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Wikimedia asks that automated clients identify themselves and provide a way
# to make contact. An anonymous or browser-spoofing agent risks a block.
USER_AGENT = (
    "aaa-launch-predictor/0.1 (research project; https://github.com/trannam97/aaa-launch-predictor)"
)

# One query per this many appids. Large enough that the whole corpus is a
# handful of requests, small enough to stay well inside the endpoint's
# query-timeout budget.
BATCH_SIZE = 50

# Between batches. The endpoint is a shared public service and this job runs
# rarely; there is no reason to lean on it.
DELAY_BETWEEN_BATCHES_SECONDS = 2.0

REQUEST_TIMEOUT_SECONDS = 120.0


class WikidataError(RuntimeError):
    """The lookup could not be completed."""


@dataclass(slots=True)
class ReleaseDates:
    """Every publication date Wikidata holds for one Steam app."""

    steam_appid: int
    dates: list[date]

    @property
    def earliest(self) -> date | None:
        return min(self.dates) if self.dates else None


def _query(appids: list[int]) -> str:
    values = " ".join(f'"{appid}"' for appid in appids)
    return f"""SELECT ?appid ?date WHERE {{
  VALUES ?appid {{ {values} }}
  ?item wdt:P1733 ?appid .
  ?item p:P577 ?stmt . ?stmt ps:P577 ?date .
}}"""


def _parse_date(raw: str) -> date | None:
    """Wikidata returns ISO timestamps; some are precise only to a year.

    A year-precision value arrives as `2016-00-00T00:00:00Z`, which is not a
    real date. Those are dropped rather than coerced to January 1st — an
    invented day could turn a same-year launch into a spurious delayed port.
    """
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class WikidataClient:
    """Batch lookups of original release dates by Steam appid."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        batch_size: int = BATCH_SIZE,
        delay_seconds: float = DELAY_BETWEEN_BATCHES_SECONDS,
        opener: object | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.batch_size = batch_size
        self.delay_seconds = delay_seconds
        # Injectable so tests can replay a recorded response without network.
        self._opener = opener

    def _fetch(self, query: str) -> dict:
        url = f"{SPARQL_ENDPOINT}?{urllib.parse.urlencode({'query': query})}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/sparql-results+json", "User-Agent": self.user_agent},
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise WikidataError(f"Wikidata query failed: {exc}") from exc

    def release_dates(self, appids: list[int]) -> dict[int, ReleaseDates]:
        """Look up every publication date for each appid.

        Appids with no Wikidata item, or none carrying P1733, are simply
        absent from the result — a miss is ordinary, not an error.
        """
        found: dict[int, ReleaseDates] = {}
        batches = [appids[i : i + self.batch_size] for i in range(0, len(appids), self.batch_size)]
        for index, batch in enumerate(batches):
            if index:
                time.sleep(self.delay_seconds)
            payload = self._fetch(_query(batch))
            for binding in payload.get("results", {}).get("bindings", []):
                try:
                    appid = int(binding["appid"]["value"])
                except (KeyError, ValueError):
                    continue
                parsed = _parse_date(binding.get("date", {}).get("value", ""))
                if parsed is None:
                    continue
                found.setdefault(appid, ReleaseDates(appid, [])).dates.append(parsed)
        return found
