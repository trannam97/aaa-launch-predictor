# Reports

Point-in-time reports on each build phase — what shipped, what the data
turned out to say, and what's still open. One file per report.

| Report | Phase | Covers |
|---|---|---|
| [`phase-0.5-launch-window-backfill.html`](./phase-0.5-launch-window-backfill.html) | 0.5 | Historical backfill of 126 AAA Steam releases with windowed launch metrics |
| [`phase-1-rubric-validation.html`](./phase-1-rubric-validation.html) | 1 | 34 games labeled from sourced research; the outcome rubric encoded and scored against them |
| [`phase-2-model-evaluation.html`](./phase-2-model-evaluation.html) | 2 | The ordinal pre-launch model built, cross-validated against a constant guess, and refused |

## Naming

`phase-<n>-<short-slug>.html` — the phase number first so the folder sorts in
build order.

## What these files are

Self-contained HTML: no build step, no external assets beyond Google Fonts,
inline CSS and JS, light and dark themes. Open one directly in a browser and
it renders.

They double as **Artifact sources**, which is why they carry no `<!DOCTYPE>`,
`<html>`, `<head>` or `<body>` tags — the Artifact publisher supplies that
wrapper. Adding them here would double-wrap on publish. Browsers construct
the missing elements themselves, so the files still open fine locally.

To publish or update one, pass its path to the Artifact tool. Republishing
the same file keeps the same URL.

## Conventions

- **Figures come from the database, not from prose.** Every number in a
  report is read out of the populated tables at write time, so a report can't
  drift from what the pipeline actually produced. Each report states the
  commit it was generated from.
- **Reports are snapshots, not living documents.** A report describes the
  state at the end of its phase. When the dataset grows, write a new report
  rather than silently editing an old one — the point of keeping them is
  being able to see what was known when.
- **The framing rules apply here too.** Outcome tiers describe commercial
  performance relative to budget-tier expectations, never creative quality,
  and every tier gets the same neutral visual treatment — no red-for-flop,
  no celebratory green. See the Responsible Framing section of
  `PROJECT_SPEC.md`. Where a chart needs to encode outcome, the four tiers
  are ordered, so they take a single hue stepped light-to-dark.
