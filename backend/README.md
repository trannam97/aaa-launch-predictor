# Backend

FastAPI app. Endpoints for listing/tracking games, reading their ingested
Steam data, and triggering a refresh. Does not itself run the scheduled jobs —
see `/jobs`.

See `PROJECT_SPEC.md` (repo root) — Backend section.

## Run it

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'                 # add '.[dev,postgres]' for Postgres

# Create the schema (defaults to a local SQLite file)
alembic upgrade head

uvicorn app.main:app --reload           # http://localhost:8000
```

Interactive API docs are at `http://localhost:8000/docs`.

With no `.env` at all this runs against `sqlite:///./aaa_launch_predictor.db`.
Point `DATABASE_URL` at Postgres for anything beyond local dev — see
`.env.example` at the repo root.

## Track a game end-to-end

```bash
curl -X POST localhost:8000/games \
  -H 'content-type: application/json' \
  -d '{"steam_appid": 1174180}'

curl localhost:8000/games/1174180 | jq
```

## Endpoints

| Method | Path | What |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/games` | Every tracked game, soonest release first |
| `POST` | `/games` | Start tracking an appid and ingest it now (200 if already tracked) |
| `GET` | `/games/{appid}` | One game plus its snapshot history (`?history_limit=`) |
| `POST` | `/games/{appid}/refresh` | Re-fetch from Steam, append a snapshot |
| `GET` | `/games/{appid}/prediction` | Pre-launch forecast — trained model if one exists, baseline otherwise; `method` says which |
| `DELETE` | `/games/{appid}` | Stop tracking and delete its snapshots |

Steam failures are mapped to HTTP: unknown appid → 404, rate limit or outage →
503, anything else from Steam → 502.

## Layout

| Module | What |
|---|---|
| `app/steam.py` | Client for the three public Steam endpoints Phase 0 uses |
| `app/ingest.py` | Upsert a game + append a snapshot; the pipeline later phases reuse |
| `app/models.py` | SQLAlchemy models, lifecycle/outcome enums |
| `app/schemas.py` | API response models and the status-badge mapping |
| `app/main.py` | Routes and Steam-error → HTTP mapping |
| `app/db.py` | Engine, request-scoped session, `session_scope()` for jobs |
| `app/cohort.py` | Cohort-normalized percentiles for counts, and price relative to each year's going rate |
| `app/rubric.py` | The outcome rubric as code: observed signals → one of four tiers |
| `app/baseline.py` | Rule-based pre-launch forecast; the fallback when no model has cleared its gate |
| `app/features.py` | The pre-launch feature contract, and the leakage guard that enforces it |
| `app/companies.py` | Company-name normalization across Steam's per-territory rights-holder spellings |
| `app/ordinal.py` | The fitted ordinal model class — lives here so a stored artifact can be unpickled |
| `app/model_forecast.py` | Serves a trained forecast when one is available; returns None when not |
| `app/validation.py` | Scores the rubric against the hand-labeled set |
| `migrations/` | Alembic revisions, beside the models they migrate |

## Tests

```bash
pytest              # from /backend
ruff check .        # from the repo root — config is in /ruff.toml
ruff format .
```

## Migrations

Alembic lives here, not in `/data`, so it can import `app.models` directly
rather than reaching across the tree — the schema's definition and its
migration history belong together. The dataset CSVs stay in `/data`, since
`/jobs` and `/ml` read them too.

```bash
cd backend

alembic upgrade head              # apply migrations
alembic downgrade -1              # roll back one
alembic check                     # do the models match the migrations?
alembic revision --autogenerate -m "add company_tier"
alembic upgrade head --sql        # print SQL instead of running it
```

The URL comes from `DATABASE_URL` (or the SQLite default) — never from
`alembic.ini`, which is committed. Batch mode is on for SQLite so the same
revision applies cleanly to hosted Postgres. `migrations/env.py` renders the
project's `UtcDateTime` as plain `sa.DateTime(timezone=True)`, so generated
migrations never import from `app` and can't be broken by a later refactor.

The suite is hermetic: an in-memory SQLite database and a stubbed Steam
transport replaying the recorded payloads in `tests/fixtures/`. No network,
no local database file, no Steam key needed.

## The prediction paths

Three pieces of code produce outcome tiers. They answer different questions
and must not be confused:

- **`app/rubric.py`** scores a game that has **already launched**, from its
  observed launch-window metrics plus post-launch studio and support signals.
  This is what validates the labeling rubric, and what the resolution job
  will eventually use to settle a "Failed to Meet Expectations" row.
- **`app/model_forecast.py`** forecasts a game that **hasn't shipped**, from
  the trained ordinal model, when one exists.
- **`app/baseline.py`** forecasts the same thing from structural signals only
  — how the same team's past day-one Steam releases resolved, plus platform
  reach. It always reports `low` confidence.

Check which one produced a number before acting on it: the API response
carries `method`.

### Why the baseline is still here

`jobs/train_model.py` refuses to write a model artifact until the model beats
a constant guess on held-out data, and at the current corpus size it does not.
So the endpoint serves `rule_based_baseline_v1` today, and starts serving
`ordinal_gbt_v1` on its own once enough labels land for the model to clear the
bar. Nothing needs to be switched over by hand.

Serving a model needs scikit-learn, which is an optional extra:

```bash
pip install -e './backend[dev,ml]'
```

Without it, `app/model_forecast.py` treats the missing stack the same way it
treats a missing artifact — no model, serve the baseline, say so in `method`.
The CI backend job deliberately installs without it, so that path stays
tested.
