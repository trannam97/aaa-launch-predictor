# Jobs

Scripts triggered on a schedule by GitHub Actions. Each one is a plain Python
entry point that can also be run by hand.

They import the backend package directly (`sys.path` is extended to
`/backend`), so they share its models, ingestion code and Steam client rather
than reimplementing them.

## Running them without a local toolchain

Four workflows dispatch these from the Actions tab, so nothing here needs
Python on your machine. They split by what they do to the data:

| workflow | runs | writes | when |
|---|---|---|---|
| **Database** | `alembic upgrade head`, `backfill_historical.py`, `detect_launch_start.py` | the database | once, to create the schema and load the corpus, and again when the launch-window correction is applied. `appids` reloads named rows in seconds rather than repeating the ~50-minute full run. |
| **Analysis** | `validate_rubric.py`, `evaluate_baseline.py` | nothing | any time. Read-only, so safe to re-run whenever labels or thresholds move. Output goes to the run's step summary. |
| **Research** | `backfill_launch_prices.py`, `draft_studio_signals.py` | a file you download | when filling curated columns. Proposes values for review; both cost quota or money per row, so both take a `limit` — `all` runs the whole queue, and only `launch-prices` accepts it. |
| **Retrain** | `train_model.py` | a model artifact | 1st and 15th, or on demand. |

The split matters when adding a job: anything that writes the database belongs
in **Database**, anything that only reports belongs in **Analysis**, and
anything that proposes a curated value belongs in **Research** — where the
output is a review file rather than a write, because those columns are curated
precisely because the automated answer is wrong often enough to matter.

All seven places that read the `DATABASE_URL` secret go through
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

### `draft_studio_signals.py`

Run it from **Actions -> Research -> `signal-drafts`**, or locally:

```
DATABASE_URL=... python jobs/draft_studio_signals.py --list          # free, no API call
DATABASE_URL=... ANTHROPIC_API_KEY=... python jobs/draft_studio_signals.py --limit 5
```

Drafts `studio_signal` and `support_signal` with Claude and web search, for the
rows the rubric refuses to call. Needs the `research` extra (`pip install -e
'./backend[postgres,research]'`).

**It writes a review file, never the database and never the curated CSV.** A
draft is not a label: a reviewer opens the sources, and only a verified value is
copied into `historical_releases.csv` by hand. The failure mode here is not a
wrong tier — it is a wrong tier with a plausible sentence attached, which is
exactly what survives a skim.

Selection runs the real rubric over unlabeled day-one releases and keeps the
ones it cannot resolve, so nothing is researched whose signals the rubric would
not have read. About 40% of unlabeled rows meet expectations and get a tier from
Steam data alone; researching those would spend a call to learn something
nothing consumes.

Output carries a `needs_attention` column. `CLOSED-verify` is the one that
matters: `closed` is a hard override straight to Flop, and it is also the value
the evidence is biased towards, since closures are announced while quiet
absorptions are not. Never merge one unread.

One Claude call with web search per game, so start with `--limit`.

### `backfill_launch_prices.py`

Run it from **Actions -> Research -> `launch-prices`**, or locally:

```
DATABASE_URL=... python jobs/backfill_launch_prices.py --list       # free, no API call
DATABASE_URL=... ITAD_API_KEY=... python jobs/backfill_launch_prices.py --limit 20
DATABASE_URL=... ITAD_API_KEY=... python jobs/backfill_launch_prices.py           # the rest
```

Proposes `launch_price_usd` from IsThereAnyDeal's Steam price history. Needs a
free key from isthereanydeal.com/apps/dev; no new Python dependency.

170 of 206 rows have no launch price, and a missing one does not read as
missing — `app/features.py` turns it into `0.0`, which the model reads as a free
game rather than an unknown. Every one of those rows carries that value into
training the moment it gets a label, which is what the signal-research queue is
about to start doing.

An LLM is the wrong tool for this: a launch price is a fact with a database
behind it, the same reason release dates come from Wikidata rather than from
asking a model.

Three constraints are structural rather than configured:

- **Standard edition** — the join is on Steam appid, and a deluxe or gold tier
  is a separate Steam app. Nothing has to filter for it.
- **US** — `country=US`, which is what `launch_price_usd` has always meant.
- **Regular price, not the sale price** — every ITAD record carries the
  undiscounted regular price alongside what was charged, so a launch-week
  discount never has to be filtered out.

Writes a **review file**, never the database and never the curated CSV. The
`verdict` column triages: `launch_price` means ITAD's earliest record sits
between 30 days before release and 60 after — the window opens early because
the request asks from before release, and the oldest record is usually the
pre-order listing, whose regular price is the launch price; `too_late` means ITAD only began tracking that title
years later, so its earliest price is a re-tier and not the launch price; and
`suspect_shape` means the parser understood only part of the response, so the
"earliest" record it found may not be the earliest at all and the number means
nothing until the shape is settled.

`--dump-raw` writes the first raw history response to a file. ITAD's history
shape is not publicly documented and the parser was written without a key, so
this captures the real shape rather than guessing at it twice.

### Run it more than once

ITAD's request allowance is small and tiered — higher once the account is
verified, and shown with recent usage on the app's page at
isthereanydeal.com/apps/dev. The first run over the whole queue got 50 games
through and then took HTTP 429 for each of the remaining 120 in twenty seconds
flat.

So the job is built to be run repeatedly rather than to fit inside one budget:

- It reads the review file back at the start and **skips games already in it**,
  so a second run continues the queue instead of repeating it. `--refresh`
  re-asks about rows already answered, keeping the rest.
- It **writes what it gathered even when it stops early**. The run that produced
  this behaviour reported "120 failed" while holding 50 correct prices.
- It **stops when the budget is gone** rather than asking 120 more times. A 429
  is no more per-row than a bad API key is.
- It asks for less: one keyless request resolves the entire queue's appids to
  ITAD ids, where asking per game used to double the traffic for no extra
  information.

In CI the review file is carried between runs by `actions/cache`, so re-running
the workflow picks up where the last one stopped. Ticking **refresh** re-asks
rather than starting over.

