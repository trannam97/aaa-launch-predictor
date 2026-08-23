# Data

Alembic migrations for the project's schema, and (from Phase 0.5) the seed
historical dataset. The live `historical_releases` table grows from that seed
as tracked games resolve — see Prediction Lifecycle in `PROJECT_SPEC.md`.

## Migrations

The models live in `/backend/app/models.py`; `migrations/env.py` imports them
from there, so there is one schema definition, not two. The database URL comes
from `DATABASE_URL` (or the backend's SQLite default) — never from
`alembic.ini`, which is committed.

```bash
cd data

alembic upgrade head              # apply migrations
alembic downgrade -1              # roll back one
alembic check                     # do the models match the migrations?
alembic revision --autogenerate -m "add historical_releases"
alembic upgrade head --sql        # print SQL instead of running it
```

Migrations use batch mode on SQLite so the same revision applies cleanly to
both local SQLite and hosted Postgres.

## Current schema

| Revision | Tables |
|---|---|
| `0001` | `games`, `game_snapshots` |

`games` holds one row per tracked release, with the three lifecycle fields
kept deliberately separate (`lifecycle_status`, the immutable
`predicted_outcome`, and the nullable `resolved_outcome`) so that "not
launched yet", "tracking fine" and "trending badly but unresolved" stay
distinguishable.

`game_snapshots` is the time series this project collects itself — review
counts, price, and concurrent players at whatever cadence the polling job
runs. There is no historical backfill available for concurrent players, so a
game's history starts the day it's added.

## Planned

- `historical_releases` — the ML training table (Phase 0.5).
- `company_tier` — clustering output, refreshed quarterly (Phase 2).
- Rollup of `game_snapshots` older than ~30 days into daily aggregates, to
  stay inside the 500MB Supabase free tier.
