# AAA Game Launch Predictor — Project Spec

## Build Status — read this before implementing anything
**Current phase: Phase 0.5 (bulk historical backfill), not started.**
Phase 0 is done — basic Steam ingestion for a single game is working.

This document is a **design reference, not a build order.** Most of it
describes decisions for phases that haven't started yet (Public Reception
Signal, company-tiering clustering, the FTME lifecycle, leak handling,
retraining cadence) — these are settled *decisions* worth having on record
so they don't need re-litigating later, not a to-do list for the current
phase. **When prompting Claude Code for the current phase, scope the ask
explicitly to what that phase needs** (see MVP Phasing below) rather than
handing over this whole document as "build this" — most of it describes
Phase 2-4 work that would be premature to scaffold now.

For Phase 0.5 specifically, the relevant sections are: Historical Labeled
Dataset, Prediction Categories (for the labeling rubric), and the seed
CSV (`historical_releases_seed.csv`). Everything under ML Model, Public
Reception Signal, Prediction Lifecycle, and Company Tiering Pipeline can
be skipped for now — they're Phase 2-3 territory.

## Goal
A dashboard that tracks upcoming Triple-A game releases on Steam and predicts
whether each will be a **Flop**, **Success**, or **Breakout Success**, using a
hybrid of a trained ML model (structural/historical features) and an LLM
reasoning layer (live qualitative signals).

## Prediction Categories (define thresholds before labeling data)
Four ordered tiers — adjust based on what data you can actually get. The
categories are ordinal (Flop < Underperform < Success < Breakout Success),
which matters for model choice (see ML Model section).
- **Flop**: doesn't recoup production costs; severe layoffs or real risk of
  studio closure; game largely abandoned post-launch (no meaningful
  patches/content); steep discounting almost immediately.
- **Underperform**: roughly breaks even or modest profit, well below
  budget-tier target; may involve layoffs but the studio continues
  operating; game still gets maintained/patched post-launch; mixed-to-
  decent reviews with a modest but real playerbase. (Mirrors the "sits
  between a downgrade and a hold" language stock analysts use — reinforces
  the neutral, forecast tone from the Responsible Framing principle.)
- **Success**: meets typical expectations for its budget tier and genre —
  respectable concurrent player peak, "Mostly Positive"+ reviews, holds
  price.
- **Breakout Success**: far exceeds expectations — concurrent player peak
  in top percentile for the genre, "Very Positive"+ reviews, sustained
  engagement weeks after launch, notable cultural/media buzz.

Budget tier matters a lot here — a $200M AAA game and a $20M "AA-leaning-AAA"
game need different bars for what counts as "success."

Distinguishing **Flop from Underperform** needs a signal beyond launch-
window numbers: whether the studio continued operating and kept
supporting the game post-launch, versus shutting down or abandoning it.
Peak concurrent players and review scores alone won't reliably separate
these two — factor in post-launch support duration and layoffs-vs-closure
reporting (see Historical Labeled Dataset and LLM Reasoning Layer below).

## Prediction Lifecycle: Forecast vs. Resolved Status
A launched game's status is not the same object as a pre-launch forecast,
and the dashboard should treat them differently:
- **Pre-launch**: a forecast — predicted category across all four tiers
  with a confidence level. Unchanged from the model above.
- **Early post-launch** (first few weeks): if trajectory trends below
  Success, status becomes **"Failed to Meet Expectations"** — provisional,
  not yet resolved to Flop or Underperform. This is the honest state of
  knowledge at that point in time; the two outcomes genuinely can't be told
  apart yet.
- **Resolution window** (tunable parameter, starting estimate 3-6 months
  post-launch): a scheduled job checks for post-launch support signals
  (patch/content cadence, layoffs vs. closure reporting) and resolves
  "Failed to Meet Expectations" into a confirmed Flop or Underperform.
- **If it never clearly resolves** (thin press coverage, quiet studio),
  leave the status at "Failed to Meet Expectations" with a note that
  there's insufficient data to resolve further, rather than forcing a
  guess. This directly supports the Responsible Framing principle above —
  it avoids applying the harshest label before it's actually confirmed.
- This mechanism also feeds Phase 4's "historical accuracy tracking" —
  comparing the pre-launch forecast against the eventually-resolved status
  is exactly what that phase needs to measure prediction accuracy over
  time.

#### Schema Design for Lifecycle vs. Outcome
Keep three things separate rather than collapsing them into one nullable
field — a bare null can't distinguish "not launched yet" from "tracking
fine" from "trending badly but unresolved," which loses real information:
- **`lifecycle_status`** (enum): `pre_launch` → `tracking` →
  `failed_to_meet_expectations` → `resolved` / `unresolved_insufficient_data`.
  Tracks where a game is in the pipeline.
- **`predicted_outcome`** + confidence: the pre-launch forecast. Set once,
  **immutable** — never overwritten later, so it can always be compared
  against what actually happened.
- **`resolved_outcome`** (enum, nullable until the resolution job fills it
  in): `flop` / `underperform` / `success` / `breakout`. This is the
  actual ground-truth label, populated only once resolved.

**Important**: "Failed to Meet Expectations" is a lifecycle status, not a
training label — it represents not-yet-knowing, not a real outcome, so it
should never be used as a class the ML model learns to predict. The
payoff of tracking it explicitly is different: once a game's
`resolved_outcome` gets populated, that row graduates from the live
tracking table into `historical_releases` — the same table the model
retrains on. This turns the resolution mechanism into a flywheel that
keeps growing the labeled training set over time, on top of its role in
accuracy tracking. Only rows with a populated `resolved_outcome` should
ever enter training — an unresolved FTME row is excluded until it settles.

#### Retraining Cadence
Retrain the ML model **biweekly, but skip the run if no new resolved
games have been added to `historical_releases` since the last retrain**.
Rationale: training a gradient-boosted tree model on a dataset this size
is computationally cheap (seconds to low minutes) — the real cost driver
in this project is Claude API usage, not model training — so the limiting
factor is data velocity, not compute. Given AAA release volume and the
3-6 month resolution window, new labeled examples will likely arrive in
occasional bursts rather than a steady weekly trickle, so a plain fixed
cadence would often run against unchanged data. The skip-if-no-new-data
check keeps the freshness ceiling without paying pipeline overhead on
empty runs, and self-adjusts to however fast resolved games actually
accumulate.

## Design Principle: Responsible Framing
This tool predicts *commercial* performance, not creative quality, and it
must never read as mockery of developers or artists. Concretely:
- **Never conflate "flop" with "bad game."** Copy and UI must make clear
  this is a sales/engagement forecast relative to budget-tier expectations
  — a critically acclaimed game can still commercially underperform, and
  the product should say so plainly where relevant.
- **Attribute outcomes to business decisions and market conditions**
  (budget, marketing spend, release timing, platform strategy), not to
  individual developers or artists. Never phrase predictions in a way that
  could be read as targeting named individuals.
- **No dunking mechanics.** No "biggest flops" leaderboard, no shareable
  mockery cards, no celebratory tone on failure predictions. All three
  categories (flop/success/breakout) get the same neutral, analyst-style
  visual treatment — think sales forecast, not hot take.
- **Be mindful of timing.** Avoid gloating or "called it" framing around
  moments tied to real harm to workers (layoffs, studio closures).
This should be a review checkpoint for all future UI copy, notification
text, and any social-sharing features.

## Architecture

### 1. Data Layer
- **Steam Web API** — store metadata, review counts/scores, tags, pricing.
  Commercial use is permitted under Steam's Web API Terms of Use; requires
  a privacy policy page (only if handling nonpublic end-user data, which
  this project doesn't) and a "not affiliated with Valve" disclaimer.
- **SteamSpy** — ownership/playtime estimates, uses Steam's public API.
- **Concurrent player counts** — poll Steam's own public current-players
  endpoint every 15-30 min and store your own history going forward. Do
  **not** scrape SteamDB — their ToS explicitly prohibits it and they
  actively block scrapers (Cloudflare challenges, IP bans). This also means
  no *historical* CCU backfill for old games; use SteamSpy ownership/
  playtime estimates and review counts/scores at launch as the historical
  proxy instead.
- **OpenCritic / Metacritic** — critic scores (check ToS/robots before
  scraping; OpenCritic has a friendlier API). Capture each review's
  **publish timestamp**, not just the score — this is what makes embargo-
  timing derivable later without a separate data source (see ML Model and
  Public Reception Signal below).
- **News/social search** (via Claude + web search, or a news API) — pre- and
  post-launch buzz, controversy, marketing cadence, and verified wishlist/
  follower announcements (see LLM Reasoning Layer below).
- Store everything in Postgres (SQLite is fine for MVP/local dev).

### 2. Historical Labeled Dataset (the hard part)
- Hand-pick ~100–200 past AAA releases spanning several years.
- Pull their launch-window stats (peak CCU, review score/count at 2 weeks,
  price cuts in first month) and apply the rubric above to assign a label.
- For each game, also check what happened afterward: did meaningful
  post-launch content/patches continue for several months, were there
  layoffs, did the studio close — this is needed to separate Flop from
  Underperform, and launch-window numbers alone won't do it.
- Store as a training table: `historical_releases`.
- Expect to iterate on the rubric once you see how games actually cluster.

### 3. ML Model
- Features: publisher/developer tier (see Company Tiering Pipeline below),
  franchise history (sequel vs new IP), genre, platform reach (PC + consoles
  vs PC-only), price point, marketing lead time, review scores of
  developer's prior titles, count of other AAA titles the same
  publisher/studio is releasing within roughly ±3 months (a resource-
  dilution/competing-attention signal distinct from general company
  scale — this is about this specific launch's conditions, not the
  company's overall tier), `platform_launch_type` (day-one Steam
  release / delayed port / former-exclusive now porting — see Release
  Date Handling below), and `embargo_timing` (early / on-time / late /
  none — derived from comparing the earliest critic review's publish
  timestamp to the release date; see Public Reception Signal below).
  **Availability caveat**: unlike the other features, `embargo_timing`
  isn't known until reviews actually start publishing, right before or at
  launch — it can't inform the pre-launch forecast, only post-launch
  monitoring, FTME resolution, and historical training.

#### Release Date Handling
Store two separate dates, not one: `steam_release_date` (when the title
hit Steam) and `original_release_date` (its true first release on any
platform). These diverge for a large share of AAA titles — e.g. Red Dead
Redemption 2 launched on console in Oct 2018 and on Steam in Dec 2019.
**Outcome tiers (flop/underperform/success/breakout) are measured against
Steam-specific performance**, matching the project's actual scope — but
the gap between the two dates is itself a meaningful feature
(`platform_launch_type`), not just metadata. A console-first title arriving
on Steam with a year of pre-existing reputation and pent-up demand is a
different prediction problem than a true day-one release with no external
signal yet — the model shouldn't be blind to that distinction, and it's a
useful hook for the LLM layer too, since a delayed port has real external
reviews/sales history to go research that a fresh release doesn't.

#### Windowed Features vs. Lifetime Snapshots
Time-sensitive features (review count, review score, concurrent players)
must be captured as snapshots at specific windows relative to release
(e.g. 2 weeks, 1 month, 3 months post-launch) and stored as first-class
historical data — never queried as "current/lifetime totals" and used as
if they represented launch performance. Lifetime numbers erase exactly the
signal being predicted: a title can read as a mediocre launch and an
excellent long-term game (again, Red Dead Redemption 2 is a clean example
of this on Steam specifically, given its delayed port).
This is achievable going forward for actively-tracked games via the
concurrent-player polling design already in this spec, extended to cover
review counts/scores the same way.

**Correction (verified against the live API during Phase 0.5):** windowed
*review* figures **are** retroactively recoverable, contrary to the earlier
assumption here. Steam's `appreviews` endpoint accepts `start_date` /
`end_date` with `date_range_type=include`, and `query_summary` then returns
counts aggregated over just that window — one request per game per window,
no pagination. Historical rows therefore carry real launch-window review
counts and scores rather than press-reported approximations. The effect is
large and not a rounding detail: Batman: Arkham Knight reads 47.7% positive
over its first two weeks against 89.7% lifetime, and Red Dead Redemption 2
70.7% against 92.5%.

What remains genuinely unrecoverable is **concurrent players**: Steam
publishes only a live count, with no historical endpoint and no permitted
scraping source. So the old/new asymmetry is real but much narrower than
described above — it applies to CCU alone, not to review data. Backfilled
rows leave `peak_concurrent_players` null rather than substituting a
present-day reading.

Two traps this exposed, both handled in the backfill:
- **Early Access.** Steam's `release_date` reports the 1.0 date, not the
  Early Access launch — Palworld reads as a 2026 release. Windows measured
  from it describe the wrong event, so `original_release_date` must be
  curated for any EA title.
- **Windows that have not elapsed.** A three-month window on a title
  released three weeks ago returns a real-looking number covering the wrong
  period. Such windows are skipped, not stored.

#### Outcome Scope: Launch, Not Eventual Fate
**Resolved in Phase 1.** Outcome tiers describe how a release *launched*, not
what it eventually became. A title that recovers years later is labeled on
its launch window: No Man's Sky is `underperform` (61.3% positive over its
first two weeks) despite a ten-year recovery to 85.0% lifetime, 200M+
revenue and forty free updates.

The reasoning is that the features the model sees are launch-window
features. Labeling against a decade-long outcome would train launch data to
predict something it cannot contain, and would make accuracy tracking
meaningless — a pre-launch forecast cannot be scored against an outcome that
took ten years to settle. Recovery is real and worth tracking eventually,
but as its own signal, not folded into the launch label.

Flop vs. Underperform still uses post-launch studio and support evidence, as
before; that evidence is about whether the shortfall was survivable, not
about eventual redemption. No Man's Sky is `underperform` rather than `flop`
because Hello Games kept operating and kept supporting the game.

#### Price Normalization (inflation)
Nominal launch price is not comparable across cohorts, for two compounding
reasons:
- **General inflation.** $60 in 2015 is worth roughly $80 in 2025 terms, so
  the industry's 2023 move from $60 to $70 was a real-terms price *cut*.
- **The industry's own pricing norm**, which matters more here. $60 was the
  standard AAA price through 2022 and $70 from 2023, so the same $60 means
  "at the going rate" in 2016 and "under it" in 2024.

Rather than deflate by a CPI series, price is expressed **relative to the
modal launch price of its cohort** — the same rolling-window machinery that
normalizes review counts. That answers the question actually being asked
(was this priced at, above, or below the going rate for its tier?), needs no
external data or annual maintenance, and avoids assuming game prices track
consumer inflation, which they demonstrably do not.

Development *cost* inflation is a separate matter, currently absorbed by
`budget_tier` being categorical rather than a dollar figure. AAA budgets have
risen faster than CPI, so if dollar figures are ever attached, they will need
their own deflator rather than reusing this one.

#### Demo Timing (and why a bare "has a demo" flag is harmful)
**Resolved in Phase 1.** A pre-launch demo plausibly moves perception —
players who try the game form a view before buying — so it is worth
capturing. But the obvious implementation is actively wrong.

Steam's `appdetails` exposes a `demos` field, which says a demo exists
*now*. Checking the demo app's own release date against the game's, across
the corpus, shows why that is not the same question: **of the titles listing
a demo, roughly two-thirds got it after launch, not before.** Publishers add
a demo to convert holdouts when sales disappoint — Immortals of Aveum 87 days
post-launch, Dragon Age: The Veilguard 34 days, Skull and Bones 279 days,
Star Wars Outlaws 147 days, all of them commercial disappointments. The
pre-launch demos skew the other way (Metaphor: ReFantazio, Resident Evil 4,
Tekken 8, Street Fighter 6).

A naive `has_demo` boolean would therefore teach a model that demos predict
failure, when what it is really seeing is a *response* to failure. Only
`demo_timing == pre_launch` may be used as a pre-launch feature. A
post-launch demo is outcome-contaminated and must be excluded from anything
forecasting a launch, though it stays recorded as a post-launch marketing
signal.

Two further limits, both stated rather than worked around:
- **Absence is not evidence.** A demo delisted after Steam Next Fest leaves
  no trace in the API, so `none_listed` means "no demo listed today", never
  "no demo existed". This is the same rule the spec already applies to
  wishlist figures.
- **Same-day demos are ambiguous** and are recorded as `launch_window` rather
  than forced into either bucket.
- **Selection effect.** Studios choose whether to demo, and that choice
  correlates with budget tier, genre and confidence in the product. Any
  association found here is descriptive, not causal — consistent with the
  Public Reception Signal guardrail against adjudicating *why* a reaction
  happened.

#### Add-on Content, and the Day-1 Patch Dead End
**Resolved in Phase 1.**

**Add-on content, split by when it shipped.** Steam's `dlc` field lists DLC
sold as separate apps, and each carries its own release date — so the same
date check used for demos splits it in two. **Launch-day DLC** (season
passes, deluxe-edition items, pre-order bonuses, dated within 3 days of
release) is a *pre-launch* monetization decision and is safe to forecast on:
Star Wars Outlaws shipped with 6, Elden Ring with 0. **Post-launch DLC** and
the gap to the most recent one describe support duration, which is a
post-launch signal and outcome-contaminated — usable for resolving an
outcome, never for predicting one.

**`dlc_count` is not a measure of content.** It counts only what is sold as a
separate Steam app. Helldivers 2 reads zero because its Warbonds are bought
with in-game currency, despite being among the most actively supported games
in the corpus; Concord also reads zero, having shipped nothing. The
`has_in_app_purchases` flag is stored alongside precisely to catch the
models the DLC list cannot see, and the two must be read together.

**Day-1 patches: investigated and rejected.** `ISteamNews/GetNewsForApp`
(public, no key, `feeds=steam_community_announcements`) reaches back past
release, so a patch announcement near launch day is detectable in principle.
Checked against known launches, it measures the wrong thing:

| Game | Launch reputation | Day-1 patch posts |
|---|---|---|
| Cyberpunk 2077 | notoriously broken | 0 |
| Redfall | notoriously broken | 0 |
| Starfield | mixed | 0 |
| Elden Ring | strong | 2 |
| Baldur's Gate 3 | strong | 1 |

The signal tracks **how communicative a studio is**, not how broken the game
was — studios having a bad launch tend to go quiet, and both infamous cases
posted nothing. Feed coverage is also inconsistent (Battlefield 2042's
history is truncated before its own release). Patch *size*, which is what
would actually discriminate, is not public. Not implemented.

#### Cohort Normalization
Raw counts (review counts, concurrent player peaks) are not comparable
across years — Steam's install base and review-leaving culture have grown
enormously over time (e.g. a AAA title's launch review count in 2015 vs.
2024 can differ by roughly two orders of magnitude purely from platform
growth, not relative success). Any count-based feature must be normalized
against a same-year (or rolling-window) cohort of comparable AAA Steam
releases — e.g. percentile rank within that cohort — rather than used raw.
This affects two places, not just model features: **rubric thresholds
themselves** (Success/Breakout Success) should be defined in cohort-
relative terms from the start, not as fixed absolute numbers, or they go
stale as Steam's baseline keeps growing and need constant manual
recalibration. This requires a small reference table of per-cohort
baselines, recomputed on a similar cadence to the company-tiering
clustering job.
- Model: gradient-boosted trees (XGBoost or LightGBM) — good fit for
  small/medium tabular datasets with mixed feature types, and gives feature
  importances for free (useful for the dashboard's "why" explanations).
  Since the four categories are ordered (Flop < Underperform < Success <
  Breakout Success), consider an ordinal-aware approach (e.g. ordinal
  logistic regression, or an ordinal wrapper around gradient-boosted trees)
  rather than plain multiclass — it respects that "predicted Underperform,
  actual Success" is a smaller miss than "predicted Flop, actual Breakout,"
  which plain multiclass classification doesn't capture.
- Output: probability distribution over {flop, underperform, success,
  breakout}.

#### Company Tiering Pipeline (sub-component, runs offline)
Budget figures aren't public, so instead of estimating dollars, derive a
categorical tier per publisher/developer via **unsupervised clustering**
(k-means or hierarchical) over observable company-level features:
- Headcount / team size (MobyGames credits, studio "about" pages).
- Catalog size (# of past AAA releases).
- Confirmed upcoming AAA titles (announced with a real release window —
  year or narrower). Weight this below past catalog size in the
  clustering: past releases are a settled fact, upcoming ones are a
  forecast that regularly slips or gets cancelled. Exclude
  rumored/leaked/vaguely-teased projects with no confirmed window.
- Platform reach on past titles.
- Average review scores of past titles.
- Revenue or market-cap bracket for public companies (used as a coarse,
  slow-changing category — **not** live stock price, which is too noisy
  and driven by unrelated business lines and market conditions).
- Run this clustering quarterly (not per-prediction), store output as a
  `company_tier` lookup table, and hand-review the resulting clusters
  before trusting them — with only a few dozen AAA publishers/developers
  to cluster, results can be noisy and deserve a sanity check rather than
  fully automated trust. The main prediction model just joins against this
  table at inference time.

### 4. LLM Reasoning Layer (Claude)
- Input: the ML model's probabilities + freshly gathered live signals
  (recent reviews, news headlines, social sentiment, marketing footprint,
  cast prominence, verified wishlist/follower announcements). For games
  already launched and being monitored post-release, also gather
  post-launch support signals (patch/content cadence, layoff reports vs.
  closure reports) needed to distinguish Flop from Underperform.
- Output: final category call + a short written rationale for the dashboard
  card, and a confidence level.
- This layer is what makes the tool explainable instead of a black box, and
  it's the piece that adapts to things the ML model can't see (a marketing
  disaster a week before launch, a surprise demo that goes viral, etc.).

#### Wishlist/Follower Signal Handling (resolved rule)
- **Platform-verified only.** Only count wishlist/follower/pre-order
  numbers when backed by an independently checkable source (e.g. a Steam
  Next Fest wishlist leaderboard placement, a platform-published dashboard
  screenshot, a specific milestone tied to a verifiable event).
- **Ignore self-reported, unverifiable claims entirely** (e.g. a studio
  tweeting "over a million wishlists!" with no backing). Do not weight
  these even lightly.
- **Absence is neutral, not a red flag.** Most developers don't publicize
  these numbers at all — the prompt for this layer should explicitly
  instruct Claude not to treat silence as a bad sign.

#### Public Reception Signal (distinct from commercial performance)
A game can hit its budget-tier commercial targets and still be broadly
disliked by buyers — this is worth tracking as its own signal, separate
from the flop/success/breakout commercial call. Design guardrail: **the
pipeline measures reception patterns descriptively; it does not classify
or adjudicate *why* a reaction happened.** Causal attribution (e.g.
whether backlash was "deserved," tied to content, or organized brigading)
is a subjective judgment call this tool should not make algorithmically —
doing so risks the tool itself appearing to take a side in gaming-culture
debates, which undermines the neutrality principle above.
- **Critic score vs. user/player score, tracked as two separate numbers**
  (OpenCritic/Metacritic for critic scores, Steam review data for user
  scores) — never blended into one. Divergence between them is itself a
  useful, transparent data point to surface, without the tool asserting
  which side is "right."
- **Review-bombing / rating-cliff detection**: flag sudden, sharp drops in
  review score and roughly when they occurred, as a factual timeline event
  — not paired with a judgment on legitimacy.
- **Trailer like/dislike ratio**: one weak, noisy input among several
  (e.g. flag when dislikes exceed ~40%). Explicitly low-confidence on its
  own — never a sole trigger for a prediction, since it's easily driven by
  factors unrelated to gameplay quality (price, DRM, unrelated controversy,
  coordinated brigading).
- **LLM rationale stays descriptive, not evaluative**: e.g. "user review
  score dropped sharply following [dated event]" rather than any claim
  about whether the reaction was fair or deserved.
- **Embargo timing** (`early` / `on-time` / `late` / `none`): computed
  from the earliest critic review's publish timestamp relative to the
  release date, using data already captured from OpenCritic/Metacritic —
  no new data source needed. This one's more concrete than the others in
  this section — a late-lifted or absent pre-launch embargo is a
  well-established, widely-cited signal in games journalism, generally
  read as reduced publisher confidence. Considered adding influencer/
  streamer sentiment alongside this, but set it aside: no clean aggregator
  exists (fragmented across platforms, not a numeric score), it's harder
  to verify than the wishlist-claim problem already solved above, and
  disclosure compliance for sponsored early access is inconsistent. Worth
  revisiting later if a good source emerges, but not now.

#### Leak Events (occurrence + reaction only, never content)
Unauthorized pre-release leaks (gameplay footage, story details, internal
builds) are a real, occasionally major pre-launch event — worth tracking
as one more entry in the existing buzz/controversy signal, with a hard
scope limit:
- **Track that a leak happened and how the public/press/market reacted —
  never the leaked content's substance.** E.g. "a significant leak
  occurred on [date], generating coverage across major outlets, with a
  temporary stock reaction" is in scope. Cataloging or summarizing what
  the leak actually revealed (game mechanics, story, map) is out of
  scope — leaked footage is frequently outdated relative to the shipping
  product, leakers often have self-interested incentives to overstate
  significance, and engaging with content obtained through unauthorized
  access carries real legal/ethical exposure distinct from citing a
  published review.
- **No curated "trusted leaker" allowlist.** Leaker reputation is
  self-reported and unverifiable, the same underlying problem as the
  wishlist-claims case — and pseudonymous leak accounts routinely get
  banned and rebrand, so a maintained list would constantly go stale.
- **Corroboration threshold instead of source trust**: only log a leak as
  a real event once it's picked up by 2+ independent mainstream outlets,
  or acknowledged by the publisher itself (takedown notices, an official
  statement, a reported stock-price reaction). This filters noise by
  independent confirmation rather than by vetting individual leakers,
  and naturally scales — a leak large enough to matter gets corroborated
  within a day or two; one nobody else picks up gets filtered out for
  free.
- **Stock-price reaction, if present, is used only as one input to the
  corroboration check** ("did something notable happen"), never as a
  structured ML feature — consistent with the earlier decision against
  using stock price for company tiering, for the same reason: it's
  noisy, moves on unrelated factors, and often reverses within days.
- This needs no new scheduled job or infrastructure — it's handled by
  the LLM reasoning layer's existing periodic buzz/controversy search
  pass, just with this as an explicit thing to check for and a boundary
  on what to do with it if found.

### 5. Backend
- Python, FastAPI.
- Scheduled job (cron or APScheduler) to refresh tracked games' data daily.
- Separate scheduled job (less frequent, e.g. monthly) to check launched
  games currently at "Failed to Meet Expectations" and attempt resolution
  to Flop/Underperform per the Prediction Lifecycle above.
- Endpoints: list tracked games, get game detail + prediction, add/remove
  tracked game, trigger re-prediction.

### 6. Frontend
- React (Next.js) dashboard.
- Card grid or table: game title, release date, predicted category badge
  pre-launch (or resolved status badge post-launch — see Prediction
  Lifecycle above), confidence, short rationale, sparkline of any trending
  metric available.
- Post-launch status badge should visually distinguish provisional
  ("Failed to Meet Expectations") from resolved (Flop/Underperform/
  Success/Breakout) states — e.g. a distinct badge style or "provisional"
  tag — so it's clear to a visitor when a game's outcome is still unsettled.
- Detail view per game showing the feature breakdown and LLM rationale.

## MVP Phasing (recommended build order)
1. **Phase 0**: Data ingestion for a handful of Steam fields (no ML/LLM yet)
   — get one game's data flowing end-to-end into the DB and rendered on a
   basic page. Deliberately built *before* the full historical research
   pass — once this pipeline exists, it can pull critic score, price,
   platforms, and review counts programmatically for any game, so the
   later bulk backfill only needs manual/LLM research for the fields that
   genuinely require judgment (studio outcome, post-launch support,
   budget tier). Doing the full research pass by hand first would mean
   redoing that work the slow way.
2. **Phase 0.5**: Bulk historical backfill — combine the Phase 0 pipeline
   (for API-fetchable fields) with targeted research (via Claude Code or
   further sessions like the seed batch already compiled) for the
   qualitative fields, to build out the ~100-200 game historical set.
   Expect the rubric to still shift a bit as real cases surface edge
   cases — that's fine, this is why Phase 1 validates on a subset before
   committing to full-scale training.
3. **Phase 1**: Manually label ~30–50 historical games (drawn from the
   Phase 0.5 backfill), build simple rule-based baseline prediction (no
   ML training yet) — validates the rubric before investing in model
   training. A rule-based baseline is what covers the gap while the
   labeled dataset is still thin, so lack of full training data doesn't
   block having a working prediction feature.
4. **Phase 2**: Train the ML model on the labeled set, wire it into the
   backend, replace the rule-based baseline.
5. **Phase 3**: Add the Claude reasoning layer on top of ML output for live
   upcoming titles.
6. **Phase 4**: Dashboard polish — tracking multiple games, refresh
   scheduling, historical accuracy tracking (did past predictions pan out?).

## Repo Setup Checklist
1. Create a new **public** GitHub repo (e.g. `aaa-launch-predictor`) — see
   Hosting & Cost above for why public matters now, not just later.
   Initialize with a `.gitignore` for Python + Node.
2. Structure the repo into clear top-level folders from the start, since
   there are now several distinct scheduled jobs and components rather
   than one simple app: `/frontend` (Next.js), `/backend` (FastAPI),
   `/ml` (training scripts, model artifacts, company-tiering clustering),
   `/jobs` (the polling, retraining, resolution, and clustering scripts
   that GitHub Actions triggers on schedule), `/data` (curated datasets),
   `/reports` (per-phase reports).
   **Revised during Phase 1:** schema migrations were originally placed in
   `/data` alongside the seed CSV. They now live in `/backend/migrations`,
   beside the models they migrate — keeping them apart forced `env.py` to
   reach across the tree to import `app.models`, and split the schema's
   definition from its history. Datasets stay in `/data`, which `/backend`,
   `/jobs` and `/ml` all read.
3. Use a database migration tool (e.g. Alembic for Python + Postgres) from
   day one rather than hand-editing schema — the schema already has
   several moving pieces (`lifecycle_status`, `predicted_outcome`,
   `resolved_outcome`, `company_tier` lookup, `historical_releases`,
   concurrent-player time-series) and will keep evolving as Claude Code
   builds it out in phases.
4. Add a `.env.example` listing required environment variables (Steam API
   key, Anthropic API key, DB connection string) without real values, and
   confirm `.gitignore` excludes the real `.env` — this matters more now
   that there are multiple API keys in play, on a public repo.
5. Add a short `DISCLAIMER.md` or footer text confirming the project isn't
   affiliated with Valve, plus a brief note on data sourcing and update
   cadence — cheap to add early, and reinforces the transparency/
   responsible-framing principle from earlier in this spec.
6. Clone the repo locally.
7. Open the repo in Claude Code and hand it this spec file as the starting
   context — ask it to scaffold Phase 0 first, not the whole app at once.
8. Get an Anthropic API key for the LLM reasoning layer (separate from
   Claude Code itself, which uses your Claude subscription).
9. Get a Steam Web API key (free, via Steam's developer portal).

## Hosting & Cost (resolved)
- **Repo visibility: public.** This is now a real cost decision, not just
  preference — GitHub Actions is free and unlimited on public repos, but
  capped at 2,000 free minutes/month on private ones (2026 pricing). The
  concurrent-player polling job (below) makes this cap easy to hit on a
  private repo. A public repo also fits the project's existing donation-
  funded, transparent-methodology framing.
- **Frontend**: Vercel free tier — well-suited to a Next.js dashboard.
- **Backend**: Render free tier (750 free web-service hours/month). Free
  tier spins down after ~15 min idle and takes 30-60s to wake on the next
  request — acceptable for a periodically-checked dashboard, not for
  instant-response needs. Railway no longer has a permanent free tier as
  of 2026 (30-day trial credit only), so Render is the better default here.
- **Database**: Supabase free Postgres — 500MB storage, auto-pauses after
  7 days with no API activity (the polling job below incidentally
  prevents this by keeping it active). 500MB requires bounding the
  concurrent-player time-series growth — see Concurrent Player Polling
  below. Alternative: Render's free Postgres, which expires after 90 days
  requiring periodic re-provisioning, if avoiding an extra provider
  matters more than the storage-growth tradeoff.
- **Scheduled ingestion**: GitHub Actions free minutes for the daily/
  periodic data-refresh job, instead of paying for an always-on worker.
- **Concurrent player polling — revised for cost given the 15-30 min
  cadence**: GitHub Actions cron isn't precisely reliable at 15-min
  intervals under load, so treat 30 min as the realistic floor. More
  importantly, poll at that frequency only during a game's active launch
  window (first few weeks, when CCU data is actually volatile and
  informative) — drop to daily or less for pre-launch and long-post-launch
  games. This bounds both Actions minutes and database growth. Roll up
  granular polling data older than ~30 days into daily aggregates rather
  than keeping it at full resolution indefinitely, to stay within the
  500MB Supabase cap as tracked games accumulate.
- **The real recurring cost is Claude API calls** for the LLM reasoning
  layer, not hosting. Keep this cheap by caching predictions and only
  re-running the LLM pass on a schedule (not per page load), and consider
  a cheaper model tier for routine signal-gathering, reserving a stronger
  model for the final category call.

## Monetization (resolved)
- **Donations only** (Ko-fi / GitHub Sponsors / Buy Me a Coffee) — low
  effort, no traffic minimums, no ad-content review. Framing it as
  covering "server + API costs" fits the hobby-dashboard model. Ads and
  affiliate links were considered and set aside (ads need traffic minimums
  this project won't have early on; Steam's affiliate program terms would
  need separate confirmation and shouldn't be assumed available).
- Donations don't conflict with Steam's Web API terms — you're running a
  service that uses the API as intended, not selling Steam's data itself.
- SteamDB scraping is off the table regardless of monetization model — see
  Data Layer above.
