# Frontend

Next.js (App Router) dashboard. Card grid of tracked games with status badges,
and a detail view per game showing its latest reading and snapshot history.

See `PROJECT_SPEC.md` (repo root) — Frontend section.

## Run it

The backend needs to be running first (see `/backend`).

```bash
npm install
echo 'NEXT_PUBLIC_API_BASE_URL=http://localhost:8000' > .env.local
npm run dev            # http://localhost:3000
```

`NEXT_PUBLIC_API_BASE_URL` defaults to `http://localhost:8000` if unset. In
production it's set in Vercel's project settings.

```bash
npm run typecheck      # tsc --noEmit
npm run build          # production build
```

## Structure

| Path | What |
|---|---|
| `app/page.tsx` | Dashboard — card grid of every tracked game |
| `app/games/[appid]/page.tsx` | Detail view — latest reading, trend, snapshot history |
| `components/StatusBadge.tsx` | Lifecycle/outcome badge |
| `components/GameCard.tsx` | Dashboard card |
| `components/Sparkline.tsx` | Inline SVG sparkline for a snapshot series |
| `lib/api.ts` | Typed fetch client for the backend |

Both pages are server components that fetch on each request
(`dynamic = "force-dynamic"`), so nothing here needs the API to be reachable
from the browser.

## UI conventions

These come from the Responsible Framing principle in `PROJECT_SPEC.md` and
apply to any copy or component added later:

- **Every outcome renders identically.** Flop, Underperform, Success and
  Breakout all use the same neutral badge — no red-for-failure, no
  celebratory green, no ranking or leaderboard treatment.
- **Provisional states look unsettled.** A game at "Failed to Meet
  Expectations" gets a dashed badge and an explicit `· provisional` tag, so a
  visitor can tell nothing has been concluded yet.
- **The framing is stated, not implied.** Header and footer both say these are
  commercial forecasts relative to budget tier, attributed to business and
  market conditions rather than to any individual's work.
