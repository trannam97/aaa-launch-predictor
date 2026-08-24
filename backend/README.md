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

# Create the schema (defaults to a local SQLite file; see /data)
cd ../data && alembic upgrade head && cd ../backend

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
| `GET` | `/games/{appid}/prediction` | Rule-based baseline forecast (Phase 1) |
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
| `app/cohort.py` | Cohort-normalized percentiles — raw counts aren't comparable across years |
| `app/rubric.py` | The outcome rubric as code: observed signals → one of four tiers |
| `app/baseline.py` | Rule-based pre-launch forecast, replaced by the model in Phase 2 |
| `app/validation.py` | Scores the rubric against the hand-labeled set |

## Tests

```bash
pytest              # from /backend
ruff check .        # from the repo root — config is in /ruff.toml
ruff format .
```

The suite is hermetic: an in-memory SQLite database and a stubbed Steam
transport replaying the recorded payloads in `tests/fixtures/`. No network,
no local database file, no Steam key needed.

## The two prediction paths

They answer different questions and must not be confused:

- **`app/rubric.py`** scores a game that has **already launched**, from its
  observed launch-window metrics plus post-launch studio and support signals.
  This is what validates the labeling rubric, and what the resolution job
  will eventually use to settle a "Failed to Meet Expectations" row.
- **`app/baseline.py`** forecasts a game that **hasn't shipped**, from
  structural signals only — how the same team's past day-one Steam releases
  resolved, plus platform reach. It has none of the launch evidence the
  rubric relies on, always reports `low` confidence, and exists so the
  dashboard has an honest floor for the trained model to beat.

Check which one produced a number before acting on it: the API response
carries `method`.
