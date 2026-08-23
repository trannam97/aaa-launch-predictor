# AAA Game Launch Predictor

A dashboard that tracks upcoming Triple-A game releases on Steam and predicts
whether each will land as a **Flop**, **Underperform**, **Success**, or
**Breakout Success**, combining a trained ML model on structural/historical
factors with an LLM reasoning layer on live signals (reviews, buzz, public
reception).

> **Status:** early development. Not yet functional — see the roadmap below.

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

## Setup

_Coming soon — Phase 0 is still in progress._

## Support

This is a hobby project run on free-tier infrastructure. If it's useful to
you, a donation helps cover the (small) recurring API costs — link coming
once the project is live.

## License

Code in this repository is licensed under the [MIT License](./LICENSE).
This license covers the code only — underlying game data belongs to its
respective sources (Valve, SteamSpy, OpenCritic, etc.) and is not owned or
relicensed by this project.
