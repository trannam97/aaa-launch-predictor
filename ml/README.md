# ML

Model training and the company-tiering pipeline. Requires `ml/requirements.txt`
on top of the backend package; **the backend itself does not depend on
scikit-learn** — it only reads the tables written here.

```bash
pip install -e ./backend '.[dev]' -r ml/requirements.txt
DATABASE_URL=... python jobs/refresh_company_tiers.py
pytest ml/tests
```

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
volume percentile, mean launch sentiment, platform breadth, active span.
These are more useful than tiers would have been: bucketing continuous
features into three categories throws away information a tree model can use
directly. The clustering was only ever a way to manufacture a categorical
where dollars were missing.

They are **not** a budget estimate. A publisher with a large catalog of
modest releases and one with a small catalog of huge ones are different
companies, and nothing here separates either from money.

## Files

| File | What |
|---|---|
| `companies.py` | Name normalization — Steam lists rights-holders per territory, so one company arrives under many spellings |
| `company_tiering.py` | Feature extraction, clustering, and the stability check that gates it |
| `tests/` | `pytest ml/tests` |

## Planned

- The ordinal outcome classifier (Phase 2 proper) — blocked on labels, not on
  code. See `jobs/evaluate_baseline.py` for the bar it has to clear.
