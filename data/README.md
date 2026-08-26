# Data

The curated historical dataset and the seed research it grew from.

Datasets live here rather than under `/backend` because three top-level
components read them: the backend, the scheduled jobs in `/jobs`, and (from
Phase 2) the training code in `/ml`. Schema **migrations** used to live here
too, but they now sit in `/backend/migrations`, beside the models they
migrate — see that folder's notes in `backend/README.md`.

## Files

| File | What |
|---|---|
| `historical_releases.csv` | **Curated input** for the backfill — one row per game, holding only what Steam cannot answer. |
| `historical_releases_seed.csv` | The original 13-game research batch, kept as provenance. Superseded by the file above; not read by any code. |

## The curated CSV

The backfill splits every row in two, and the split is the point:

- **API-derived** — name, studio, genres, Steam release date, platforms,
  critic score, and all windowed review figures. Refetched from Steam on
  every run, never hand-edited.
- **Curated** — the true first-release date, budget tier, launch price,
  post-launch support, studio outcome, and the outcome label. Steam cannot
  know these. Only the CSV writes them.

Re-running the backfill therefore refreshes the machine half in place
without ever overwriting research.

Two of the curated fields exist in both prose and structured form.
`studio_outcome` and `post_launch_support` are for a human reading the row;
`studio_signal` (`grew`/`continued`/`severe_layoffs`/`closed`) and
`support_signal` (`sustained`/`curtailed`/`abandoned`) are what the rubric
runs on, because a rule cannot read prose. They are deliberately separate
axes: a studio can be gutted and still finish the season pass (The Callisto
Protocol), and a healthy studio can walk away from a title (Marvel's
Avengers). Telling Flop from Underperform needs both.

`research_status` tracks the qualitative pass: `not_researched` (API data
only), `researched` (a label is set), `unresolvable` (someone looked and the
public record doesn't settle it). The loader rejects a `resolved_outcome`
without `research_status=researched`, and vice versa, so an uncited label
can't slip into ground truth. **Only rows with a populated
`resolved_outcome` are eligible for training.**

## Windowed, not lifetime

Review figures are captured at 2 weeks, 1 month, and 3 months from the Steam
release, plus a lifetime row for reference — never lifetime alone. Steam's
`appreviews` endpoint accepts a date range and returns aggregated counts for
it in a single request, so this works retroactively for games released years
ago.

It matters more than it sounds. Measured over their first two weeks versus
lifetime:

| Game | Launch 2wk | Lifetime |
|---|---|---|
| Batman: Arkham Knight | 47.7% positive | 89.7% |
| Monster Hunter: World | 59.5% | 88.8% |
| No Man's Sky | 61.3% | 85.0% |
| TEKKEN 8 | 75.2% | 56.4% |

A model trained on lifetime numbers would see Arkham Knight's broken PC
launch as a well-received release.

## Labels

35 of 205 rows carry an outcome label, each with a confidence level and cited
sources. Distribution: 8 flop, 13 underperform, 6 success, 8 breakout.

**Labels describe the launch, not the eventual outcome.** No Man's Sky is
`underperform` — 61.3% positive over its first two weeks — despite a ten-year
recovery to 85.0% lifetime. The features are launch-window features, so
labeling against a decade-long outcome would train them to predict something
they cannot contain. Recovery is out of scope for now.

**Labels are Steam-scoped.** Per the spec's Release Date Handling, outcome
tiers measure Steam-specific performance, so only day-one Steam releases are
labeled. A delayed port's commercial story happened on another platform
months or years earlier, and labeling it would teach a model backwards —
Titanfall 2's Steam launch reviews at 96% positive while its "underperform"
outcome belongs to a 2016 Origin release. Ports carry
`original_release_date` and `platform_launch_type` but no label, and three
seed rows whose labels predate this rule are flagged in their notes and
excluded from rubric validation.

**MMOs and free-to-play titles are not labeled.** Cohort normalization ranks
raw launch review counts within a release year, which only means the same
thing across titles sold the same way. An F2P or subscription MMO has a
different acquisition funnel entirely: no price barrier, a launch that is
designed to build over months, and a review count that reflects install base
rather than sales. Ranking one against premium releases would put it high on
the volume axis for reasons unrelated to the thing the tier is measuring.

Concretely, exclude a title from labeling when Steam tags it **Massively
Multiplayer**. Four titles in the corpus carry that tag today — New World:
Aeternum, Diablo IV, Dune: Awakening, Riders Republic — and all four are
unlabeled, so the rule costs nothing at present. It is written down so it stays
a decision rather than an accident, and `app/features.py` enforces it.

Three things this rule deliberately does **not** cover:

- **Live-service premium games are still labeled.** Concord, Suicide Squad and
  Marvel's Avengers are flops; Helldivers 2 is a breakout. Same model, opposite
  outcomes — that is signal, and removing it would remove the extremes the
  corpus most needs.
- **`has_in_app_purchases` is not a live-service marker.** Steam sets it for
  Hogwarts Legacy, a premium single-player RPG with a purchasable deluxe
  upgrade.
- **The `Free To Play` tag is not a scope rule.** Steam tags Halo Infinite
  free-to-play for its multiplayer client, while the paid campaign is the thing
  being labeled. Excluding on it silently drops a real AAA release — which is
  exactly what happened the first time this rule was written.

That is three separate Steam fields — `dlc_count`, `has_in_app_purchases`, and
the `Free To Play` genre — that each look like a design category and each turn
out to describe a **storefront listing**. Treat any Steam flag as a statement
about the store page until proven otherwise.

An exclusion earns its place by what the label would mean, never by what it
does to a score. Filtering the training set until the numbers improve, on a
corpus this size, finds noise and calls it a finding — see the Evaluation
Protocol notes in `ml/README.md`.

## Original release dates come from Wikidata

`original_release_date` is what separates a day-one Steam launch from a port
that arrived years after the console release — and Steam cannot supply it,
since it only knows its own date. Without it `derive_platform_launch_type`
has nothing to compare against and every row falls to `unknown`, which is
where 166 of these rows sat.

`jobs/enrich_release_dates.py` fills it from **Wikidata**, joining on P1733
(Steam application ID) — a structured lookup, not scraping. The earliest P577
publication date across all platforms is taken as the original release.

Coverage on this corpus: **164 of 166**, resolving to 102 day-one launches and
61 delayed ports. The two misses are Call of Duty: Black Ops 6 and Modern
Warfare III, whose Wikidata items don't carry a Steam appid; they stay
`unknown` rather than being guessed.

Three things the job refuses to write rather than approximate:

- **Year-precision dates.** Wikidata renders these as `2016-00-00`. Coercing
  one to January 1st could turn a same-year launch into a spurious port.
- **Rows where Steam predates the earliest known release** by more than the
  seven-day tolerance. That means one of the two sources is wrong, and
  classifying it anyway would bury the problem. None occurred on this corpus.
- **`platform_launch_type` itself.** The job writes only the date;
  `app/backfill.py` stays the single place that decides day-one versus port.

Per-platform qualifiers (P400) exist but are inconsistently populated, so they
are deliberately unused — the minimum publication date is sufficient. A 2014
console launch against a 2019 Steam date identifies a port whether or not
either statement names its platform.

## Known caveats

- **Concurrent players can't be backfilled.** Steam publishes only a live
  count and SteamDB scraping is off the table, so `peak_concurrent_players`
  stays null on historical rows rather than being filled with a present-day
  reading that means something else.
- **Steam's release date is the *Steam* release.** Red Dead Redemption 2
  shipped on console in Oct 2018 and on Steam in Dec 2019. `platform_launch_type`
  captures the gap, derived from the two dates, and can be overridden in the
  CSV to mark a former platform exclusive (which dates alone can't reveal).
- **Early Access reports the 1.0 date.** Palworld reads as a 2026 release
  because Steam surfaces its full-release date, not its Jan 2024 EA launch.
  Any EA title needs `original_release_date` curated before its windows mean
  anything. Palworld's row is flagged in the CSV.
- **The API's price is today's price.** `price_overview.initial` is the
  current list price, not the launch price — The Witcher 3 launched at
  $59.99 and now lists at $39.99. Launch price is curated for that reason;
  the API value is kept separately as `current_list_price_cents`.
- **Critic scores are patchy.** Steam carries a Metacritic score for 72 of
  126 games. OpenCritic is needed to close the gap.
- **Delisted games need a hand-supplied appid.** `appdetails` still returns
  full data for them (Concord and Marvel's Avengers both backfilled fine),
  but store *search* won't surface them, and `GetAppList` now requires a
  Steam Web API key. Babylon's Fall is the one seed game left out for this
  reason.
- **Nominal prices aren't comparable across years either.** $60 was the
  standard AAA price through 2022 and $70 from 2023, and $60 in 2015 is worth
  roughly $80 in 2025 terms — so the industry's price rise was a real-terms
  cut. `launch_price_cents` stores the nominal figure; `PriceIndex` in
  `app/cohort.py` expresses it relative to the modal price of its cohort.
  That index only produces a rate for cohorts with enough curated prices,
  which today means the recent years — it improves as more rows get labeled.
- **A demo listed today usually wasn't there at launch.** Steam's `demos`
  field reports current state, and across this corpus roughly two-thirds of
  listed demos were added *after* release — publishers converting holdouts
  when sales disappoint. `demo_timing` classifies each against the demo app's
  own release date, and only `pre_launch` is safe as a pre-launch feature.
  `none_listed` means no demo is listed now, never that none existed: Next
  Fest demos are routinely delisted.
- **`dlc_count` counts Steam apps, not content.** Helldivers 2 reads zero
  because Warbonds are bought with in-game currency. Read it together with
  `has_in_app_purchases`. The useful split is `launch_day_dlc_count` (a
  pre-launch monetization decision, safe to forecast on) versus
  `post_launch_dlc_count` and `last_dlc_days_after_launch` (support duration,
  outcome-contaminated).
- **Counts aren't comparable across years.** The Witcher 3 drew 7,519 launch
  reviews in 2015; Black Myth: Wukong drew 689,276 in 2024. That's Steam's
  growth, not a 90x difference in success. `cohort_year` is stored so
  count-based features can be ranked within a same-year cohort; the
  normalization itself is Phase 2 work.

## Schema

The tables these CSVs load into are defined in `/backend/app/models.py` and
migrated from `/backend/migrations`. Run migrations from `/backend`:

```bash
cd backend && alembic upgrade head
```

| Revision | What it adds |
|---|---|
| `0001` | `games`, `game_snapshots` |
| `0002` | `historical_releases`, `release_windows` |
| `0003` | `studio_signal`, `support_signal` on `historical_releases` |
| `0004` | demo presence and timing (`has_demo`, `demo_released_before_launch`) |
| `0005` | add-on content by timing (`launch_day_dlc_count`, `post_launch_dlc_count`, `has_in_app_purchases`) |
| `0006` | `release_date_changes` — announced-date slippage, observed forward only |
| `0007` | `publisher_stats` — per-publisher aggregates; `tier` NULL until clustering is trustworthy |

## Planned

- `company_tier` — clustering output, refreshed quarterly (Phase 2).
- Per-cohort baselines for count normalization (Phase 2).
- Rollup of `game_snapshots` older than ~30 days into daily aggregates.
