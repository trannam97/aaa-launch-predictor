# Jobs

Scripts triggered on a schedule by GitHub Actions. Each one is a plain Python
entry point that can also be run by hand.

They import the backend package directly (`sys.path` is extended to
`/backend`), so they share its models, ingestion code and Steam client rather
than reimplementing them.

## Available now

### `refresh_tracked_games.py`

Re-fetches Steam data for tracked games and appends a snapshot to each.

```bash
# Refresh everything already in the database
DATABASE_URL=... python jobs/refresh_tracked_games.py

# Add or refresh specific games
DATABASE_URL=... python jobs/refresh_tracked_games.py --appid 1174180 --appid 2000950
```

It waits ~1.5s between games (`--delay` to change) to stay well under Steam's
soft limit of roughly 200 store requests per 5 minutes per IP, and exits
non-zero if any game failed, so a scheduled run surfaces as a failed run
rather than passing quietly.

### `backfill_historical.py`

Phase 0.5. Reads `data/historical_releases.csv`, fetches each game's Steam
data plus its windowed review figures, and upserts the merged row into
`historical_releases`.

```bash
# Backfill everything in the CSV
DATABASE_URL=... python jobs/backfill_historical.py

# A few games, without writing
DATABASE_URL=... python jobs/backfill_historical.py --appid 2443720 --dry-run
```

Four Steam requests per game (details plus three review windows), paced at
2s between games. Re-running refreshes the API-derived fields in place and
leaves curated research alone. Warnings are printed but not fatal — a name
mismatch between CSV and Steam usually means a wrong appid, and a
"has not elapsed yet" warning means the game is too recent for that window.

## Planned

Per `PROJECT_SPEC.md`:

- **Concurrent-player polling** — every 30 min during a game's active launch
  window, dropping to daily for pre-launch and long-post-launch games. 30 min
  is the realistic floor; GitHub Actions cron isn't reliable at 15.
- **Snapshot rollup** — collapse polling data older than ~30 days into daily
  aggregates to bound database growth.
- **FTME resolution** — monthly; resolves "Failed to Meet Expectations" into
  Flop or Underperform from post-launch support signals, or leaves it
  unresolved when the evidence isn't there.
- **Retraining** — biweekly, skipped when no new resolved games have landed in
  `historical_releases` since the last run.
- **Company tiering** — quarterly clustering refresh, hand-reviewed before the
  new tiers are trusted.

No GitHub Actions workflow schedules these yet — that lands with the hosted
database, so scheduled runs don't start failing against a `DATABASE_URL` that
doesn't exist.
