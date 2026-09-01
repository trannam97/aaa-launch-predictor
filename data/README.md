# Data

The curated historical dataset and the seed research it grew from.

Datasets live here rather than under `/backend` because three top-level
components read them: the backend, the scheduled jobs in `/jobs`, and (from
Phase 2) the training code in `/ml`. Schema **migrations** used to live here
too, but they now sit in `/backend/migrations`, beside the models they
migrate — see that folder's notes in `backend/README.md`.

## Files

| File | What |
|---|---|
| `historical_releases.csv` | **Curated input** for the backfill — one row per game, holding only what Steam cannot answer. |
| `historical_releases_seed.csv` | The original 13-game research batch, kept as provenance. Superseded by the file above; not read by any code. |
| `game_list.csv` | **Generated snapshot**, not input. One row per game with the tier currently assigned and how it was arrived at. Read by nothing; regenerate rather than edit. |
| `signal_drafts.csv` | **Unverified research output**, not input. Drafted studio and support signals awaiting human review. Read by nothing; values move to the curated CSV only after a reviewer opens the sources. |

### `game_list.csv` is a snapshot, and will go stale

It holds name, Steam year, tier, and basis for all 206 rows, where basis is one
of: **confirmed** (a hand-researched label in the curated CSV), **auto** (the
rubric resolved it from Steam data with studio and support forced unknown),
**needs research**, **port**, **flagged**, or **no data**. At the time of
writing that is 35 confirmed, 45 auto, 69 needing research, 52 ports, 4
flagged and 1 inert.

There is no job that produces it, because the view it shows properly belongs to
the database: once `backfill_historical.py` has run, the same four columns come
from `historical_releases` joined to `release_windows`. This file was assembled
before that database exists, by running `app.rubric.classify` over launch
windows fetched live from Steam. Treat it as a point-in-time read for reviewing
labels by eye, and regenerate it from the database rather than editing it —
every value in it is derived, so an edit here changes nothing downstream and
only disagrees with the pipeline.

## The curated CSV

The backfill splits every row in two, and the split is the point:

- **API-derived** — name, studio, genres, Steam release date, platforms,
  critic score, and all windowed review figures. Refetched from Steam on
  every run, never hand-edited.
- **Curated** — the true first-release date, budget tier, launch price,
  post-launch support, studio outcome, and the outcome label. Steam cannot
  know these. Only the CSV writes them.

Re-running the backfill therefore refreshes the machine half in place
without ever overwriting research.

Two of the curated fields exist in both prose and structured form.
`studio_outcome` and `post_launch_support` are for a human reading the row;
`studio_signal` (`grew`/`continued`/`severe_layoffs`/`closed`) and
`support_signal` (`sustained`/`curtailed`/`abandoned`) are what the rubric
runs on, because a rule cannot read prose. They are deliberately separate
axes: a studio can be gutted and still finish the season pass (The Callisto
Protocol), and a healthy studio can walk away from a title (Marvel's
Avengers). Telling Flop from Underperform needs both.

`research_status` tracks the qualitative pass: `not_researched` (API data
only), `researched` (a label is set), `unresolvable` (someone looked and the
public record doesn't settle it). The loader rejects a `resolved_outcome`
without `research_status=researched`, and vice versa, so an uncited label
can't slip into ground truth. **Only rows with a populated
`resolved_outcome` are eligible for training.**

## Windowed, not lifetime

Review figures are captured at 2 weeks, 1 month, and 3 months from the Steam
release, plus a lifetime row for reference — never lifetime alone. Steam's
`appreviews` endpoint accepts a date range and returns aggregated counts for
it in a single request, so this works retroactively for games released years
ago.

It matters more than it sounds. Measured over their first two weeks versus
lifetime:

| Game | Launch 2wk | Lifetime |
|---|---|---|
| Batman: Arkham Knight | 47.7% positive | 89.7% |
| Monster Hunter: World | 59.5% | 88.8% |
| No Man's Sky | 61.3% | 85.0% |
| TEKKEN 8 | 75.2% | 56.4% |

A model trained on lifetime numbers would see Arkham Knight's broken PC
launch as a well-received release.

## Labels

35 of 205 rows carry an outcome label, each with a confidence level and cited
sources. Distribution: 8 flop, 13 underperform, 6 success, 8 breakout.

**Labels describe the launch, not the eventual outcome.** No Man's Sky is
`underperform` — 61.3% positive over its first two weeks — despite a ten-year
recovery to 85.0% lifetime. The features are launch-window features, so
labeling against a decade-long outcome would train them to predict something
they cannot contain. Recovery is out of scope for now.

**Labels are Steam-scoped.** Per the spec's Release Date Handling, outcome
tiers measure Steam-specific performance, so only day-one Steam releases are
labeled. A delayed port's commercial story happened on another platform
months or years earlier, and labeling it would teach a model backwards —
Titanfall 2's Steam launch reviews at 96% positive while its "underperform"
outcome belongs to a 2016 Origin release. Ports carry
`original_release_date` and `platform_launch_type` but no label, and three
seed rows whose labels predate this rule are flagged in their notes and
excluded from rubric validation.

**MMOs and free-to-play titles are not labeled.** Cohort normalization ranks
raw launch review counts within a release year, which only means the same
thing across titles sold the same way. An F2P or subscription MMO has a
different acquisition funnel entirely: no price barrier, a launch that is
designed to build over months, and a review count that reflects install base
rather than sales. Ranking one against premium releases would put it high on
the volume axis for reasons unrelated to the thing the tier is measuring.

Concretely, exclude a title from labeling when Steam tags it **Massively
Multiplayer**. Four titles in the corpus carry that tag today — New World:
Aeternum, Diablo IV, Dune: Awakening, Riders Republic — and all four are
unlabeled, so the rule costs nothing at present. It is written down so it stays
a decision rather than an accident, and `app/features.py` enforces it.

Three things this rule deliberately does **not** cover:

- **Live-service premium games are still labeled.** Concord, Suicide Squad and
  Marvel's Avengers are flops; Helldivers 2 is a breakout. Same model, opposite
  outcomes — that is signal, and removing it would remove the extremes the
  corpus most needs.
- **`has_in_app_purchases` is not a live-service marker.** Steam sets it for
  Hogwarts Legacy, a premium single-player RPG with a purchasable deluxe
  upgrade.
- **The `Free To Play` tag is not a scope rule.** Steam tags Halo Infinite
  free-to-play for its multiplayer client, while the paid campaign is the thing
  being labeled. Excluding on it silently drops a real AAA release — which is
  exactly what happened the first time this rule was written.

That is three separate Steam fields — `dlc_count`, `has_in_app_purchases`, and
the `Free To Play` genre — that each look like a design category and each turn
out to describe a **storefront listing**. Treat any Steam flag as a statement
about the store page until proven otherwise.

An exclusion earns its place by what the label would mean, never by what it
does to a score. Filtering the training set until the numbers improve, on a
corpus this size, finds noise and calls it a finding — see the Evaluation
Protocol notes in `ml/README.md`.

## Original release dates come from Wikidata

`original_release_date` is what separates a day-one Steam launch from a port
that arrived years after the console release — and Steam cannot supply it,
since it only knows its own date. Without it `derive_platform_launch_type`
has nothing to compare against and every row falls to `unknown`, which is
where 166 of these rows sat.

`jobs/enrich_release_dates.py` fills it from **Wikidata**, joining on P1733
(Steam application ID) — a structured lookup, not scraping. The earliest P577
publication date across all platforms is taken as the original release.

Coverage on this corpus: **164 of 166**, resolving to 102 day-one launches and
61 delayed ports. The two misses are Call of Duty: Black Ops 6 and Modern
Warfare III, whose Wikidata items don't carry a Steam appid; they stay
`unknown` rather than being guessed.

Three Wikidata conventions have to be honoured or the dates come out wrong,
and each was found the hard way by a date that changed when it shouldn't have:

- **Time precision.** Wikidata renders a year-precision value as **January 1st
  of that year** — a real-looking date, not an obviously-broken one. Saints Row
  carried `2021-01-01` at precision 9 beside its actual 2022-08-23 launch.
  Only day precision (11) is accepted.
- **Statement rank.** A superseded or slipped date is marked deprecated:
  Forspoken holds `2022-05-24` and `2022-10-11` that way beside its real
  2023-01-24 release. Deprecated statements are filtered out in the query.
- **Preferred rank versus Early Access.** Baldur's Gate 3 holds its 2020-10-06
  Early Access date at normal rank and every 1.0 platform date at preferred.
  Taking the plain minimum dates the game three years early. Where an item
  marks preferred statements, those win — which also correctly gives Hades
  2020-09-17 rather than its 2018 Early Access launch.
- **Editions are separate items.** Wikidata models *Horizon Zero Dawn Complete
  Edition* as its own item carrying only 2020-08-07, while the game it is an
  edition of carries 2017-02-28, its PS4 launch. Without following P629
  ("edition or translation of") back to the base game, a three-year-old
  console title reads as a day-one PC launch. This is the case the whole hop
  exists for.

Two further things the job refuses to write rather than approximate:
- **Rows where Steam predates the earliest known release** by more than the
  seven-day tolerance. That means one of the two sources is wrong, and
  classifying it anyway would bury the problem. None occurred on this corpus.
- **`platform_launch_type` itself.** The job writes only the date;
  `app/backfill.py` stays the single place that decides day-one versus port.

Per-platform qualifiers (P400) exist but are inconsistently populated, so they
are deliberately unused — the minimum publication date is sufficient. A 2014
console launch against a 2019 Steam date identifies a port whether or not
either statement names its platform.

### It is not a window anchor

`original_release_date` answers *when did this title first exist*, not *when
did it go on sale on Steam*. Those are the same date for most rows and wildly
different for the rest, so anchoring a measurement window on it fails quietly
rather than loudly — the query succeeds and returns nothing.

Measured on this corpus: 59 of the 205 rows with a usable Steam date carry a
gap past the seven-day tolerance, reaching five years at the extreme. Halo:
The Master Chief Collection is 2014-11-11 on Wikidata and 2019-12-03 on Steam;
God of War is 2018-04-20 against 2022-01-14; Titanfall 2 is 2016-10-28 against
2020-06-18. Opening a fourteen-day launch window on the Wikidata date returns
**zero reviews for 56 rows**. Re-anchored on Steam's own date, 55 of those 56
return a populated window — Halo alone has 42,514 launch-fortnight reviews
that the wrong anchor reports as none.

The pipeline already anchors correctly and needs no guarding: `app/backfill.py`
measures every window from `launch_window_start or details.release_date` and
sets `cohort_year` from the Steam year, while `original_release_date` is read
only to derive the launch type and to date award nominations, which genuinely
want the title's first release. The trap is for analysis written *against the
CSV directly*, where `original_release_date` is the only date column present
and looks like the one to use. It is not. Fetch the Steam date first.

## Pre-launch anticipation (captured, not yet used)

`prelaunch_award_nominations` and `prelaunch_award_wins` count nominations in
award categories that judge games **before they exist**. `jobs/enrich_award_nominations.py`
fills them from Wikidata across every such show it could find, not just the
biggest one:

| Award | Nominations in corpus |
|---|---|
| The Game Awards − Most Anticipated Game | 27 |
| Golden Joystick Awards − Most Wanted Game | 6 |
| Japan Game Awards − Future Category | 1 |
| Gamescom Award − Most Wanted (incl. platform variants) | 2 |

This is one of very few signals here that is **verifiably** pre-launch rather
than assumed to be: Wikidata stamps each nomination with the date it was made,
so the ordering is proven per row. Two rules protect that guarantee:

- **Undated nominations are dropped**, never assumed to precede release.
- **Year-precision dates only count when the entire year precedes the release
  year.** Gamescom and Golden Joystick statements frequently carry only a year,
  rendered as January 1st; comparing that day directly would count a November
  nomination as preceding a June release in the same year.

The cutoff is `original_release_date`, not the Steam date — anticipation is
about a game that exists nowhere yet, so a console launch ends it even when the
Steam version is years off.

**These columns are not features.** `app/features.py` does not read them. Nine
of the 32 trainable rows carry a nomination, which is far too thin to evaluate:

| Outcome | Corpus | Nominated |
|---|---|---|
| Flop | 25% | **0%** |
| Underperform | 34% | 56% |
| Success | 16% | 11% |
| Breakout | 25% | 33% |

Zero flops among nine nominated games is suggestive — Concord, the corpus's
clearest flop, was never nominated — but at a 25% base rate, seeing none in
nine has roughly a 7% chance of happening anyway. Promoting it to a feature on
that basis would be the same error as filtering the training set until the
numbers improve. It is captured now because it can only be captured *before*
launch; it gets evaluated when the labeled set can support it.

One limit worth recording: this measures **press and jury anticipation, not
consumer intent**, and absence conflates "nobody wanted it" with "nobody
covered it."

## `steam_release_date` is not always the day the game launched

Every windowed metric here is measured from `steam_release_date`, taken from
Steam's `appdetails`. An audit — checking each row for reviews dated *before*
its recorded release — found **18 of 204** rows with 50 or more, which means
those windows are measured over the wrong fortnight. They fail silently, with
plausible-looking numbers.

Three separate causes, and only one is a defect:

**1. Early Access graduating to 1.0 (working as intended).** Baldur's Gate 3
carries 8,166 reviews before its 2023-08-03 date, Hades 651, Grounded 436.
Those are reviews of the Early Access build, and the launch window correctly
starts at 1.0 — the same rule that makes Wikidata's preferred-rank date the
right one. Nothing to fix.

**2. Tiered early access — now handled.** Deluxe and premium editions
routinely unlock three to five days before the standard edition, and Steam's
release date is the *standard* date. Those buyers are playing the finished
**1.0 build** and reviewing it, inside a window we were excluding.

The project's rule is that a launch is the 1.0 version. A premium head start
ships 1.0 and counts; an Early Access build is not 1.0 and does not.
`jobs/detect_launch_start.py` separates them using the one property that
divides them reliably — an Early Access tail runs for months, a head start for
days — and writes `launch_window_start` where they differ. The backfill then
measures every window from it.

**There is a third case the probes cannot resolve, and it is left unwritten.**
A pre-order *beta* also puts reviews in the days before release, and nothing
in the review counts distinguishes it from a premium tier shipping 1.0:

Head starts longer than a week are therefore reported and **not written**,
since two to five days is the premium-tier pattern and anything beyond that
needs a person to look. Confirming one is a manual edit.

The case that established this is **Warhammer 40,000: Darktide**: 20,908
reviews across the thirteen days before its store date, which looks exactly
like a premium tier unlocking early. It was a pre-order beta — four missions
of an unfinished build — with 1.0 arriving on the store date as listed. By the
1.0 rule those reviews belong outside the window, so it keeps its store date.

**Ports are not probed at all.** They are excluded from labeling and from the
cohort, so nothing reads their windows: probing them spends Steam requests to
produce a number no one consumes, and puts rows on the needs-confirming list
that are not worth anyone's time. Rise of the Tomb Raider was flagged for a
12-day discrepancy before this rule existed — and it launched on Xbox in
November 2015, so it is a delayed port whichever date you pick, and the
question never mattered. Skipping ports removes 122 Steam requests per run.

The scale of what was being dropped, against a window opened seven days
earlier:

| Game | Recorded 2wk | Opened 7d earlier | Cohort percentile |
|---|---|---|---|
| Warhammer 40,000: Space Marine 2 | 51,977 | 81,303 | 86.7 → 92.4 |
| Sid Meier's Civilization VII | 16,920 | 30,363 | 67.7 → 77.1 |
| Avowed | 4,834 | 6,911 | 32.3 → **45.8** |

Volume percentile is a primary rubric input (`VOLUME_FLOOR`, `MOMENTUM_VOLUME`,
`BREAKOUT_VOLUME`), and Avowed crosses the 35-point floor in that shift. This
systematically under-counts exactly the largest releases, since they are the
ones that ship premium tiers.

**3. A release date that is simply wrong.** Call of Duty: Black Ops 6 shipped
25 Oct 2024; its Steam store page says 1 Nov. The recorded window holds 1,925
reviews against 6,478 for the true launch fortnight — a 23-point percentile
error.

A blanket seven-day shift is **not** the fix: it would pull Early Access
reviews into the launch window for category 1, which is precisely what the
1.0 rule exists to prevent. Distinguishing the cases is tractable — an Early
Access tail runs for months, a premium head start for days — but it changes
what "launch window" means, so it is recorded here rather than quietly
applied.

## Four launch types the dates cannot settle

`derive_platform_launch_type` compares `steam_release_date` against
`original_release_date` and calls anything past the seven-day tolerance a
delayed port. That is right for a console-first title and wrong for two other
shapes, both of which put a large date gap on a game that never left Steam.

**Early Access graduations.** The project's rule is that a launch is the 1.0
version, so an Early Access period ends at a day-one Steam launch. Baldur's
Gate 3 and Hades already work this way and need no curation, because their
Wikidata items carry 1.0 dates — BG3 is labeled `breakout` on a 2023-08-03
launch after three years in Early Access. Palworld, Grounded and Starship
Troopers: Extermination differ only in that Wikidata dates *them* from their
Early Access start, so the gap reads as a port. That is a data inconsistency,
not a different kind of release, and they are curated to `day_one_steam` to
match.

**A former store exclusive.** Metro Exodus launched on PC via Epic on
2019-02-15 and returned to Steam sale on 2020-02-14. Steam pre-orders placed
before the exclusivity deal was announced were honoured, so the game accrued
6,677 Steam reviews across the 13-month gap while no new copies could be
bought. Neither date bounds a representative launch window — the earlier one
sees only the pre-order cohort, the later one a re-listing into a year of
existing reputation. It is curated `former_exclusive`, which keeps it out of
the cohort and out of labeling rather than dating it to whichever is
convenient.

### Why this is curated rather than detected

The obvious probe is to look for reviews before the recorded date, which
`launch_window.py` already does. It cannot separate these two cases. Measured
month by month across each game's gap:

| game | gap | months with reviews | reviews before the Steam date |
|---|---|---|---|
| Palworld | 31 months | 31 / 31 | 425,126 |
| Grounded | 27 months | 27 / 27 | 35,629 |
| Starship Troopers: Extermination | 18 months | 18 / 18 | 27,984 |
| Metro Exodus | 13 months | 13 / 13 | 6,677 |

All four sold continuously on Steam for the whole gap, so no threshold on
volume, spread or recency tells an Early Access build apart from a shipped
game whose store listing was closed to new buyers. What separates them is the
commercial arrangement behind the listing, which no Steam field reports —
exactly the case `platform_launch_type` exists in the curated CSV to carry,
and `app/backfill.py` prefers a curated value over the derived one.

## The cohort is day-one launches only

Cohort normalization ranks a launch against its peers from the same year. The
reference set is **day-one Steam releases only** — delayed ports and former
exclusives are excluded, and so are rows whose launch type is still unknown.

A port's Steam window is not a launch. It measures whatever PC audience
remained after the console release already happened, often years earlier:
median 2,740 reviews against 9,188 for day-one releases in this corpus, a
3.4x gap. With 60 of 204 reference rows carrying that, the distribution was
dragged down and every day-one game's percentile inflated.

The effect was not small. On the 32 labeled rows, removing ports moved
percentiles by 6.5 points on average and up to 19.6, and **12 rows crossed a
rubric threshold** — all four flops dropped below `VOLUME_FLOOR`, where they
had been sitting above it.

Crucially the rubric got *better*, with no threshold retuned:

| Cohort | Met-expectations | Exact | Ordinal distance |
|---|---|---|---|
| All releases (previous) | 100% | 93.8% | 0.06 |
| Day-one only | 100% | **96.9%** | **0.03** |

(The launch-window correction that followed took exact agreement to 90.6%, and
running it against the live database returned 93.8% — one row, Clair Obscur,
sitting a point from a threshold. See the Retention Thresholds note in
`PROJECT_SPEC.md`. Met-expectations stayed at 100% throughout.)

### The one remaining disagreement is a label, not a rubric gap

Suicide Squad is hand-labeled `flop` where the rubric says `underperform`, and
it is tempting to read that as the flop/underperform separator being too
lenient. Grouping every labeled row by the two signals that separator reads
says otherwise:

| studio signal | support signal | hand labels |
|---|---|---|
| closed | abandoned | flop ×3 |
| continued | abandoned | flop ×2 |
| **continued** | **curtailed** | **flop ×1, underperform ×5** |
| continued | sustained | underperform ×5, success ×5, breakout ×2 |
| severe layoffs | abandoned | flop ×1 |
| severe layoffs | curtailed | flop ×1 |
| severe layoffs | sustained | underperform ×2 |

Suicide Squad is the lone `flop` in a signal class where five other rows are
`underperform`. The rubric is making the majority call for that class; the hand
label is the exception, argued from commercial magnitude — a ~$200M write-down
and a 41% fall in WB's game division revenue — rather than from studio fate or
support. Changing the rubric to agree with it would break the five rows it
currently gets right, on a sample of one.

What the row actually shows is that the flop/underperform separator measures
*consequence* (did the studio die, was the game abandoned) while the label
sometimes measures *magnitude*. Both are defensible; they are not the same
question, and only one of them is answerable from signals we can research
consistently.

One narrower thing is worth fixing, and is not tuning: the rubric's fall-through
branch reports "studio continued operating and the game kept being supported"
for any row that reaches it, including `curtailed` ones like this. The tier is
defensible, the sentence is not — it asserts something the data contradicts.

That is the evidence this is a correction rather than a recalibration: the
thresholds were tuned against the contaminated distribution and still improved
against the clean one.

`PriceIndex` applies the same rule for the same reason — a years-old game
arriving on PC at a discount says nothing about that year's going rate for a
new release. The measured effect there is much weaker, flipping only 2024 and
by a single vote in a near-tie (8x $60 against 7x $70), so it is applied for
consistency of principle rather than on the strength of its own evidence. It
also exposes a real limitation: the modal price is unstable in a year when the
market is mid-transition between price points, and the mode discards that
ambiguity rather than reporting it.

## Regional reach — tracked games only, never backfilled

`Game.regional_offers` records which markets can buy a title and at what
price, sampled across eight countries spanning purchasing power and known
restriction zones. A closed market removes an entire audience, which is a real
constraint on sales and therefore on review volume: Red Dead Redemption 2,
Monster Hunter World and Modern Warfare III are all unpurchasable in Russia
today, and Steam reports that as `success: false` rather than an error.

Regional pricing is equally available — Space Marine 2 is $59.99 in the US,
R$199.90 in Brazil, ¥9,800 in Japan, and $39.99 **in US dollars** in Turkey,
where Valve repriced to stop currency arbitrage.

**Neither is on `historical_releases`, and that is deliberate.** Both are live
readings. RDR2 *was* on sale in Russia when it launched in 2019 — the
withdrawal came in 2022 — and today's Brazilian price is not the launch price.
Backfilling a historical row from either would be anachronistic by years: the
same present-state trap that already caught lifetime reviews, current price
and DLC counts. It is captured for *tracked upcoming* games, where the reading
is genuinely pre-launch, and refreshed at most fortnightly since a market
withdrawal is not a daily event.

## Studio closure is not studio failure

`studio_signal` exists to capture whether a studio survived its launch, and
the obvious reading of a headline is often wrong. **Max Payne 3** is the case
to remember:

- Metacritic 87, critically acclaimed.
- Take-Two said it **underperformed** in its Q1 FY2013 results — roughly
  440,000 US units in the first month against about 3 million shipped, leaving
  millions of unsold units on shelves.
- Rockstar Vancouver closed two months after launch.

The first two make it a textbook case of *critical success is not commercial
success*, which is the whole reason this project measures sales-shaped
outcomes rather than review scores. But the third is **not** evidence of that:
Rockstar Vancouver was merged into Rockstar Toronto and staff were offered
positions at the expanded Oakville site. It was consolidation, not collapse.

Coding that row `studio_signal=closed` would be wrong, and it is exactly the
mistake a researcher reading "developer closes two months after launch" would
make. Reserve `closed` for a studio that actually stopped existing with its
people out of work; a merger with retained staff is `continued`.

**This is the highest-stakes field in the schema.** `closed` is a hard
override: the rubric returns Flop at high confidence immediately, without
consulting volume, sentiment, retention or support. Measured across the 32
labeled day-one rows, a wrongly-set `closed` flips **12 of them to Flop, and
every one is currently Underperform** — the error lands exactly on the
boundary this field exists to decide, and only ever pushes one way.

Only 3 of 35 labeled rows are `closed`. When the evidence is a headline
rather than a staff outcome, code `unknown`: with studio and support both
unknown the rubric returns *unresolved*, which is the correct answer to
"we don't know" and far cheaper than a false Flop.

(Max Payne 3 itself is not in the corpus: it launched in 2012, outside the
2014–2025 range, and its Steam release trailed console by 16 days, making it a
delayed port either way.)

## Early Access reviews are recoverable, and pre-launch

Under the 1.0 rule, Early Access reviews sit before launch, and windowed
`appreviews` retrieves them for any date range. That makes them unusual: a
pre-launch signal that is not lost to history.

Late-EA sentiment tracked 1.0 launch sentiment to within 0.1 points on both
cases available — Baldur's Gate 3 (97.1% -> 97.2%) and Hades (98.5% ->
98.6%) — while volume transferred not at all. **n=2, so a hypothesis, not a
finding.**

It is not a feature and will not become one: only five corpus rows are
genuinely Early Access and one is labeled, because Early Access is an indie
and AA practice while this corpus is AAA. And "do EA games launch better"
cannot be answered from data of this shape at all — games that fail in Early
Access never reach 1.0, so the comparison sees only survivors.

Useful per game in Phase 3, for a tracked title currently in Early Access.

## Known caveats

- **Concurrent players can't be backfilled.** Steam publishes only a live
  count and SteamDB scraping is off the table, so `peak_concurrent_players`
  stays null on historical rows rather than being filled with a present-day
  reading that means something else.
- **Steam's release date is the *Steam* release.** Red Dead Redemption 2
  shipped on console in Oct 2018 and on Steam in Dec 2019. `platform_launch_type`
  captures the gap, derived from the two dates, and can be overridden in the
  CSV to mark a former platform exclusive (which dates alone can't reveal).
- **Early Access reports the 1.0 date.** Palworld reads as a 2026 release
  because Steam surfaces its full-release date, not its Jan 2024 EA launch.
  Any EA title needs `original_release_date` curated before its windows mean
  anything. Palworld's row is flagged in the CSV.
- **The API's price is today's price.** `price_overview.initial` is the
  current list price, not the launch price — The Witcher 3 launched at
  $59.99 and now lists at $39.99. Launch price is curated for that reason;
  the API value is kept separately as `current_list_price_cents`.
- **Critic scores are patchy.** Steam carries a Metacritic score for 72 of
  126 games. OpenCritic is needed to close the gap.
- **Delisted games need a hand-supplied appid.** `appdetails` still returns
  full data for them (Concord and Marvel's Avengers both backfilled fine),
  but store *search* won't surface them, and `GetAppList` now requires a
  Steam Web API key. Babylon's Fall is the one seed game left out for this
  reason.
- **Nominal prices aren't comparable across years either.** $60 was the
  standard AAA price through 2022 and $70 from 2023, and $60 in 2015 is worth
  roughly $80 in 2025 terms — so the industry's price rise was a real-terms
  cut. `launch_price_cents` stores the nominal figure; `PriceIndex` in
  `app/cohort.py` expresses it relative to the modal price of its cohort.
  That index only produces a rate for cohorts with enough curated prices,
  which today means the recent years — it improves as more rows get labeled.
- **A demo listed today usually wasn't there at launch.** Steam's `demos`
  field reports current state, and across this corpus roughly two-thirds of
  listed demos were added *after* release — publishers converting holdouts
  when sales disappoint. `demo_timing` classifies each against the demo app's
  own release date, and only `pre_launch` is safe as a pre-launch feature.
  `none_listed` means no demo is listed now, never that none existed: Next
  Fest demos are routinely delisted.
- **`dlc_count` counts Steam apps, not content.** Helldivers 2 reads zero
  because Warbonds are bought with in-game currency. Read it together with
  `has_in_app_purchases`. The useful split is `launch_day_dlc_count` (a
  pre-launch monetization decision, safe to forecast on) versus
  `post_launch_dlc_count` and `last_dlc_days_after_launch` (support duration,
  outcome-contaminated).
- **Counts aren't comparable across years.** The Witcher 3 drew 7,519 launch
  reviews in 2015; Black Myth: Wukong drew 689,276 in 2024. That's Steam's
  growth, not a 90x difference in success. `cohort_year` is stored so
  count-based features can be ranked within a same-year cohort; the
  normalization itself is Phase 2 work.

## Schema

The tables these CSVs load into are defined in `/backend/app/models.py` and
migrated from `/backend/migrations`. Run migrations from `/backend`:

```bash
cd backend && alembic upgrade head
```

| Revision | What it adds |
|---|---|
| `0001` | `games`, `game_snapshots` |
| `0002` | `historical_releases`, `release_windows` |
| `0003` | `studio_signal`, `support_signal` on `historical_releases` |
| `0004` | demo presence and timing (`has_demo`, `demo_released_before_launch`) |
| `0005` | add-on content by timing (`launch_day_dlc_count`, `post_launch_dlc_count`, `has_in_app_purchases`) |
| `0006` | `release_date_changes` — announced-date slippage, observed forward only |
| `0007` | `publisher_stats` — per-publisher aggregates; `tier` NULL until clustering is trustworthy |

## Planned

- `company_tier` — clustering output, refreshed quarterly (Phase 2).
- Per-cohort baselines for count normalization (Phase 2).
- Rollup of `game_snapshots` older than ~30 days into daily aggregates.
