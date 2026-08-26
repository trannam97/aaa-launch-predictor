# ML

Model *fitting* and *evaluation*. Requires `ml/requirements.txt` on top of the
backend package; **the backend itself does not depend on scikit-learn** — it
falls back to the rule-based baseline when the stack is absent.

```bash
pip install -e './backend[dev,ml]' -r ml/requirements.txt
DATABASE_URL=... python jobs/train_model.py --report-only --verbose
DATABASE_URL=... python jobs/refresh_company_tiers.py
pytest ml/tests
```

## What lives where

The split follows one rule: **anything both training and serving need lives in
the backend**, because two implementations of "the feature vector" would drift
and the symptom would be a quietly wrong forecast rather than an error.

| Module | Home | Why |
|---|---|---|
| `app/features.py` | backend | The pre-launch feature contract. Training builds a matrix from it; the API builds one row per request. Pure SQLAlchemy — no scikit-learn. |
| `app/companies.py` | backend | Name normalization, used by both. Pure stdlib. |
| `app/ordinal.py` | backend | The fitted model class. An artifact can only be unpickled where its classes are importable, and the API runs from `backend/`. |
| `app/model_forecast.py` | backend | Loads an artifact if there is one; returns None if there isn't, which is the normal case. |
| `ml/train.py` | here | Cross-validation, the comparison against a constant, and the gate. |
| `ml/company_tiering.py` | here | Clustering and the stability check that gates it. |

## The outcome model — measured, and gated on beating a constant

Frank & Hall ordinal decomposition over gradient-boosted trees: three binary
classifiers (`> flop`, `> underperform`, `> success`) recombined into a
distribution. Ordinal because Flop < Underperform < Success < Breakout is an
ordered scale — mistaking a breakout for a success is a small error and
mistaking it for a flop is a large one, and a plain 4-way classifier scores
both as simply wrong.

**Twelve pre-launch features**, and the separation from post-launch data is
enforced in code rather than by discipline. `FORBIDDEN_FIELDS` names the
columns that describe what happened at or after launch — review volume, launch
sentiment, retention, studio fate, Metacritic — and `assert_no_leakage()`
raises if one reaches the matrix. This matters more than it sounds: every one
of those fields would raise measured accuracy while destroying the model's
purpose, and the failure would be invisible in the accuracy number.

**Evaluation is repeated stratified k-fold, never a single holdout.** At this
corpus size an eight-row test set moves ~12 points per row, so a single split
reports its own fold assignment rather than the model. Every row is scored
while held out, averaged over 20 repeats.

**The bar is a constant guess, not the rule-based baseline.** The baseline does
not clear the constant either (32.3% against 35.5%), so beating it would prove
nothing. Two trivial guesses are used, each the strongest one for its metric:
the modal tier for accuracy, the median tier for ordinal distance.

`beats_constant()` requires the ordinal-distance improvement to clear its own
95% interval — being ahead on the mean is not evidence when the interval
straddles zero — and forbids an accuracy regression. `jobs/train_model.py`
writes no artifact unless it passes, and deletes a stale one that no longer
does. Until then `/games/{appid}/prediction` serves the baseline, tagged
`rule_based_baseline_v1` so a reader can tell.

## Company tiering — currently produces no tiers, on purpose

The spec's plan: budget figures aren't public, so cluster publishers on
observable behaviour and use the cluster as a categorical budget proxy. Run
quarterly, store as a lookup table, hand-review before trusting.

**Measured on the real 204-game corpus, the clustering does not hold up, and
the job refuses to write tiers.** Across 26 publishers:

| Check | Result | Bar |
|---|---|---|
| Silhouette (best k) | 0.26 | 0.35 |
| Seed agreement (adjusted Rand) | 0.33 | 0.75 |

Different random seeds produce substantially different partitions —
Activision lands with Ubisoft in 5 runs of 12, Blizzard in 3. Worse, the
split runs the wrong way: tier correlates **−0.67** with mean review score
and **−0.60** with mean launch volume, so the "major" tier is just the
prolific-but-middling publishers. That is a performance profile, not a budget
tier, and using it to predict performance would be circular.

Three of the spec's six intended features are unavailable, and they are the
ones carrying actual budget information:

| Feature | Status |
|---|---|
| Catalog size | Available (but reflects *our corpus*, not the company's real catalog) |
| Average review score of past titles | Available |
| Platform reach | Available but useless — Steam OS flags aren't console reach; correlates +0.00 |
| Headcount / team size | **Missing** — needs MobyGames credits or studio pages |
| Confirmed upcoming titles | **Missing** — corpus is historical |
| Revenue / market-cap bracket | **Missing** — needs per-company research |

`stability()` re-checks this on every run, so if the corpus ever develops real
structure the tiers start being written without anyone editing code. Until
then `publisher_stats.tier` stays NULL. **Do not fill it in by hand.**

## What is written instead

`publisher_stats` holds the aggregates themselves — catalog size, mean launch
volume percentile, mean launch sentiment, platform breadth, active span. They
describe the corpus, and they are worth having for that.

**The model does not read them**, and cannot. A stored aggregate is computed
over every release a publisher has, including the game being predicted and
every game they shipped afterwards — so using it would leak a row's own
outcome into its own features and let a 2019 launch be forecast from a 2024
track record. `app/features.py` recomputes the record per row with both
excluded. That is not something a cached table or a cluster tier can express.

They are **not** a budget estimate. A publisher with a large catalog of
modest releases and one with a small catalog of huge ones are different
companies, and nothing here separates either from money.

## Files

| File | What |
|---|---|
| `train.py` | Cross-validation, the constant comparison, the gate, and the label digest the retraining job skips on |
| `company_tiering.py` | Feature extraction, clustering, and the stability check that gates it |
| `tests/` | `pytest ml/tests` |

## Serving

Three of the twelve features are recorded during historical backfill but are
not ingested for an upcoming release: budget tier, whether a demo shipped
before launch, and day-one DLC count. `build_live_features()` fills them with
defaults and returns the list of what it assumed, which the forecast reports
in its `basis`. A forecast resting mostly on defaults should not read the same
as one resting on evidence.

A stored model is refused rather than served when its `feature_names` no
longer match the current contract — the columns would silently misalign and
nothing about the output would look wrong.

## Planned

- **More labels.** The corpus holds 204 games and 34 labels. Everything above
  is in place and measured; what it needs is data, and no amount of modelling
  substitutes for it.
- **Artifact distribution.** `retrain.yml` measures on schedule but does not
  deploy — the API reads a model from its own disk, and there is no deployment
  target yet.
