# Jobs

Scripts triggered on a schedule by GitHub Actions. Each one is a plain Python
entry point that can also be run by hand.

They import the backend package directly (`sys.path` is extended to
`/backend`), so they share its models, ingestion code and Steam client rather
than reimplementing them.

## Running them without a local toolchain

Three workflows dispatch these from the Actions tab, so first-time setup needs
no Python on your machine:

| workflow | runs | when |
|---|---|---|
| **Database** | `alembic upgrade head`, then `backfill_historical.py` | once, to create the schema and load the corpus. Its `appids` input reloads named rows in seconds instead of repeating the ~50-minute full run. |
| **Analysis** | `validate_rubric.py`, `evaluate_baseline.py` | any time. Read-only, so safe to re-run whenever labels or thresholds move. Output goes to the run's step summary. |
| **Retrain** | `train_model.py` | 1st and 15th, or on demand. |

All four places that read the `DATABASE_URL` secret go through
`.github/actions/database-url`, a composite action that normalises the scheme,
masks the URL and every credential fragment inside it, and refuses a password
containing an unencoded `@` before opening a connection. It exists as one
shared action rather than a copy per workflow because an earlier per-workflow
copy masked only the leading fragment of a password, and a failed connection
printed the rest into a build log.

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

Currently reports the model at 39.8% against 35.5% for a constant guess, and
ordinal distance 0.96 against 0.90 — so it writes nothing, and the baseline
keeps serving.

Needs `ml/requirements.txt`. See `ml/README.md` for the evaluation protocol
and why the bar is a constant rather than the baseline.

### `enrich_release_dates.py`

Fills `original_release_date` from Wikidata (joined on P1733, the Steam app ID)
so `derive_platform_launch_type` has something to compare against. Without it,
166 of 205 rows sat at `unknown` and were excluded from labeling and training.

```bash
DATABASE_URL=... python jobs/enrich_release_dates.py --dry-run
DATABASE_URL=... python jobs/enrich_release_dates.py --refresh   # re-derive existing dates
```

`--refresh` exists because the lookup itself improves: following P629 to a base
game, and honouring statement rank and time precision, corrected 17 dates that
the first pass got wrong. See `data/README.md` for those conventions.

### `enrich_award_nominations.py`

Counts pre-release award nominations across every show that judges unreleased
games — The Game Awards, Golden Joystick, Gamescom, Japan Game Awards.

```bash
DATABASE_URL=... python jobs/enrich_award_nominations.py --dry-run --verbose
DATABASE_URL=... python jobs/enrich_award_nominations.py
```

Stored, not used: too few labeled rows carry a nomination to evaluate it. Run
it against tracked upcoming games — that is the only time the data exists.

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
