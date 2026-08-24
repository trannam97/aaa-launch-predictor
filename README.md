# AAA Game Launch Predictor

A dashboard that tracks upcoming Triple-A game releases on Steam and predicts
whether each will land as a **Flop**, **Underperform**, **Success**, or
**Breakout Success**, combining a trained ML model on structural/historical
factors with an LLM reasoning layer on live signals (reviews, buzz, public
reception).

> **Status:** Phase 1 — 126 historical releases backfilled, 34 hand-labeled
> from sourced research, and the outcome rubric encoded and validated against
> them. Predictions come from a rule-based baseline; no trained model or LLM
> reasoning layer yet. See the roadmap below.

## Disclaimer

This project is **not affiliated with, endorsed by, or connected to Valve
Corporation** in any way. All Steam data is retrieved via Valve's public Web
API in accordance with its Terms of Use.

Predictions here reflect **commercial/engagement performance relative to
budget-tier expectations, not creative or artistic quality.** A game can be
critically excellent and still commercially underperform, or vice versa —
see `PROJECT_SPEC.md` for the full methodology, including how outcomes are
categorized and what data informs each prediction.

## What it predicts

| Category | Meaning |
|---|---|
| **Flop** | Doesn't recoup costs; severe layoffs or real risk of studio closure; game largely abandoned post-launch. |
| **Underperform** | Breaks even or modest profit, well below target; studio continues operating and supporting the game. |
| **Success** | Meets typical expectations for its budget tier and genre. |
| **Breakout Success** | Far exceeds expectations — commercially and/or culturally. |

Newly-launched games that are trending below Success are labeled
**"Failed to Meet Expectations"** as a provisional status until enough
post-launch data exists to resolve them into Flop or Underperform — see the
Prediction Lifecycle section of `PROJECT_SPEC.md`.

## Data sources

Steam Web API, SteamSpy, OpenCritic/Metacritic, and web/news search for
qualitative signals. No scraping of SteamDB (see `PROJECT_SPEC.md` for why).

## Methodology

Full architecture, rubric, and design decisions live in
[`PROJECT_SPEC.md`](./PROJECT_SPEC.md) — kept in the repo intentionally so
the prediction logic stays inspectable rather than a black box.

## Tech stack

FastAPI (backend) · Next.js (frontend) · Postgres via Supabase · XGBoost/
LightGBM (ML model) · Claude (LLM reasoning layer) · GitHub Actions
(scheduled jobs)

## Repo layout

| Folder | What |
|---|---|
| `/backend` | FastAPI app — Steam client, ingestion pipeline, API, DB migrations |
| `/frontend` | Next.js dashboard |
| `/data` | Curated datasets shared by the backend, jobs and ML code |
| `/jobs` | Scripts GitHub Actions runs on a schedule |
| `/ml` | Model training and company-tiering clustering (Phase 2) |
| `/reports` | Per-phase reports on what shipped and what the data showed |

Each folder has its own README with details.

## Quickstart

Nothing here needs an API key — the Steam endpoints Phase 0 uses are public,
and the database defaults to a local SQLite file.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 2. Schema
alembic upgrade head

# 3. API on :8000
uvicorn app.main:app --reload

# 4. Track a game (in another shell)
curl -X POST localhost:8000/games \
  -H 'content-type: application/json' \
  -d '{"steam_appid": 1174180}'

# 5. Dashboard on :3000 (in another shell)
cd frontend && npm install && npm run dev
```

For a hosted database, copy `.env.example` to `.env` and set `DATABASE_URL`.

## Roadmap

| Phase | Status | What |
|---|---|---|
| 0 | **Done** | Steam ingestion end-to-end: one game's data into the DB and onto a page |
| 0.5 | **Done** | Historical backfill — 126 releases (2015–2025) with windowed launch metrics |
| 1 | **Done** | 34 games labeled from sourced research; rubric encoded and validated at 96.8% on its falsifiable axis; rule-based baseline wired to the API |
| 2 | Next | Train the ordinal gradient-boosted model on the labeled set, replace the baseline |
| 3 | | Claude reasoning layer on top of model output for live titles |
| 4 | | Dashboard polish, refresh scheduling, historical accuracy tracking |

## Development

```bash
cd backend && pytest        # backend suite (hermetic — no network, no DB file)
ruff check . && ruff format --check .   # from the repo root
cd frontend && npm run typecheck && npm run build
```

## Support

This is a hobby project run on free-tier infrastructure. If it's useful to
you, a donation helps cover the (small) recurring API costs — link coming
once the project is live.

## License

Code in this repository is licensed under the [MIT License](./LICENSE).
This license covers the code only — underlying game data belongs to its
respective sources (Valve, SteamSpy, OpenCritic, etc.) and is not owned or
relicensed by this project.
