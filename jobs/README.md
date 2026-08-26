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

### `validate_rubric.py`

Phase 1's checkpoint. Scores `app/rubric.py` against every hand-labeled
day-one Steam release and prints where it disagrees.

```bash
DATABASE_URL=... python jobs/validate_rubric.py
DATABASE_URL=... python jobs/validate_rubric.py --json
DATABASE_URL=... python jobs/validate_rubric.py --min-agreement 90   # gate CI
```

Read the disagreements rather than the headline number — they are where the
rubric is still wrong. Delayed ports are excluded from scoring: their label
describes a launch that happened on another platform, so scoring them against
a Steam window measures nothing.

### `refresh_company_tiers.py`

Quarterly. Writes `publisher_stats` aggregates always; writes cluster tiers
only if the clustering passes a stability check, which it currently does not.
Prints the cluster table either way for the hand-review the spec asks for.
Needs `ml/requirements.txt`. See `ml/README.md` for why tiers are empty.

### `evaluate_baseline.py`

Scores the *pre-launch* baseline against the labeled set, leave-one-out.
Distinct from `validate_rubric.py`, which scores the *post-launch* rubric —
they solve different problems and must not be compared to each other.

Currently reports the baseline at 32.3% against 35.5% for always guessing the
most common outcome: structural pre-launch features carry little signal at
this sample size. That constant, not the baseline, is the bar Phase 2 has to
clear.

### `train_model.py`

Biweekly. Fits the ordinal outcome model, cross-validates it, and writes an
artifact **only if it beats a constant guess on held-out data**.

```bash
DATABASE_URL=... python jobs/train_model.py
DATABASE_URL=... python jobs/train_model.py --report-only --verbose
DATABASE_URL=... python jobs/train_model.py --force     # after a feature change
```

Two early exits, both normal outcomes rather than failures. **No new labels**:
the run compares a digest of (appid, outcome) pairs against the last run's and
skips when it matches, per the spec's Retraining Cadence. `--force` overrides
it, which is what you want after changing the feature set or the model, since
neither moves the label digest. **The model loses to a constant**: no artifact
is written, any stale one is removed, and `/games/{appid}/prediction` keeps
serving the rule-based baseline tagged as such.

Needs `ml/requirements.txt`. See `ml/README.md` for the evaluation protocol
and why the bar is a constant rather than the baseline.

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
- **Company tiering** — quarterly clustering refresh, hand-reviewed before the
  new tiers are trusted.

`retrain.yml` is the only scheduled workflow so far. The rest land with the
hosted database, so scheduled runs don't start failing against a
`DATABASE_URL` that doesn't exist.
