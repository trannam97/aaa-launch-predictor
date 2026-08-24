# Data

Alembic migrations, the curated historical dataset, and the seed research it
grew from.

## Files

| File | What |
|---|---|
| `historical_releases.csv` | **Curated input** for the Phase 0.5 backfill — one row per game, holding only what Steam cannot answer. |
| `historical_releases_seed.csv` | The original 13-game research batch, kept as provenance. Superseded by the file above; not read by any code. |
| `migrations/` | Alembic revisions. Models live in `/backend/app/models.py`. |

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

34 of 126 rows carry an outcome label, each with a confidence level and cited
sources. Distribution: 7 flop, 12 underperform, 7 success, 8 breakout.

**Labels are Steam-scoped.** Per the spec's Release Date Handling, outcome
tiers measure Steam-specific performance, so only day-one Steam releases are
labeled. A delayed port's commercial story happened on another platform
months or years earlier, and labeling it would teach a model backwards —
Titanfall 2's Steam launch reviews at 96% positive while its "underperform"
outcome belongs to a 2016 Origin release. Ports carry
`original_release_date` and `platform_launch_type` but no label, and three
seed rows whose labels predate this rule are flagged in their notes and
excluded from rubric validation.

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
- **Counts aren't comparable across years.** The Witcher 3 drew 7,519 launch
  reviews in 2015; Black Myth: Wukong drew 689,276 in 2024. That's Steam's
  growth, not a 90x difference in success. `cohort_year` is stored so
  count-based features can be ranked within a same-year cohort; the
  normalization itself is Phase 2 work.

## Migrations

The database URL comes from `DATABASE_URL` (or the backend's SQLite
default) — never from `alembic.ini`, which is committed.

```bash
cd data

alembic upgrade head              # apply migrations
alembic downgrade -1              # roll back one
alembic check                     # do the models match the migrations?
alembic revision --autogenerate -m "add company_tier"
alembic upgrade head --sql        # print SQL instead of running it
```

Batch mode is on for SQLite so the same revision applies cleanly to both
local SQLite and hosted Postgres. `migrations/env.py` renders the project's
`UtcDateTime` as plain `sa.DateTime(timezone=True)`, so generated migrations
never import from `app` and can't be broken by a later refactor.

| Revision | Tables |
|---|---|
| `0001` | `games`, `game_snapshots` |
| `0002` | `historical_releases`, `release_windows` |
| `0003` | `studio_signal`, `support_signal` on `historical_releases` |

## Planned

- `company_tier` — clustering output, refreshed quarterly (Phase 2).
- Per-cohort baselines for count normalization (Phase 2).
- Rollup of `game_snapshots` older than ~30 days into daily aggregates.
