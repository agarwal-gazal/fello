# Decision log

Running notes, written as each call was made. The README is distilled from this.

## D1. Validate the synthetic data before building on it (day 1)
First version of the generator had almost no learnable signal: a logistic regression on eight as-of features scored
AUC 0.62 against a ceiling of 0.81 (knowing the hidden health exactly), and single features sat at 0.45 to 0.58.
A churn model trained on that would have "worked" only by accident. Steepened how health drives usage, tickets,
replies and payment failures, enlarged the planted accounts so the decline is visible, re-measured: 0.67 (0.70 on
mid-market + enterprise) vs the same 0.81 ceiling. Kept the gap: some churn is deliberately unpredictable (2 % exogenous).
*Trade-off:* a synthetic world flatters whatever model I build if I tune it to be easy. The ceiling number is how I keep myself honest.

## D2. Product telemetry is a sample (day 1)
~200k events cannot hold full telemetry for 5,000 accounts, so product counts are relative activity, never absolute.
`--usage-scale` raises density without touching logic. Stated in the data dictionary so no feature treats counts as truth.

## D3. Ground truth is physically separate (day 1)
`data/ground_truth/` is read only by validation and evaluation, never by the pipeline. Includes an entity-link answer key
so entity resolution can be scored, and planted cases (two lost renewals, two decoys).

## D4. Raw layer is verbatim, append-only, idempotent (day 1)
- `payload jsonb` untouched; the only typed column is `synced_at` (knowledge time), NOT NULL, because every
  point-in-time feature must filter on it. A record without it fails the load rather than being guessed at.
- Identity is `(tenant_id, md5(raw line))`: reloading an export is a no-op, but source-side duplicates (webhook retries,
  Zendesk ticket versions, re-sent product events) carry a different `_synced_at`, so they are KEPT. Deduplication is a
  business rule and belongs in staging, where it is visible and testable, not buried in the loader.
- One transaction per file: a bad file leaves everything else loaded and nothing half-loaded; rerun resumes.
- Unknown source entities are rejected, not skipped: silent schema drift is how pipelines rot.
*Cost:* the raw tables are ~2x the size of a deduplicated load and every consumer must remember to read through staging.

## D5. Loader shells out to psql `\copy` instead of using a Python driver (day 1)
`COPY` is the fastest bulk path (290k rows in ~14 s) and keeps the loader dependency-free. Also forced by the build
sandbox, where PyPI is unreachable (proxy 403), so no Postgres driver could be installed. Lines go through COPY CSV with
control characters as quote/delimiter, because COPY's default text format mangles backslashes in JSON (tested).
The application layer will need a real driver; that is a deploy-time `pip install`, not a design constraint.

## D6. Entity resolution: evidence ladder with confidence, never a forced match (day 2)
- Every link stores `method`, `confidence`, `conflict` and, when unlinked, a `reason`. A CSM-facing number can always be traced
  back to "this ticket was attributed to this account because of X".
- Ladder: explicit key (1.0) > website/contact domain (.90-.95) > person's email (.90) > workspace user vote (.80) > name (.85) > fuzzy (.70).
  Name is consulted whenever the best company evidence is below .85, not only when there is none.
- **A personal address (gmail.com...) is a person, not a company.** Free-mail person matches are capped at .75 so company evidence overrules
  them. Before this, 9 records were wrongly attributed that way.
- **Wrong is worse than missing.** A bad link feeds another account's features; an unlinked ticket only lowers coverage. Free-mail senders
  with no person match stay unlinked (about 550 of 12.7k tickets, 4%), they are reported, not guessed.
- Salesforce dedupe: same website domain; same normalised name unless websites conflict; typo duplicates (one edit apart, slug length >= 8,
  no website conflict) corroborated by a shared contact domain, or by the same owner unless the difference is a single substituted letter
  ("Ivory Ward" vs "Ivory Yard" are different companies; this rule merged them once, the test now guards it).
- Learned domains: an alias domain seen on an already-linked record becomes evidence for later records, only if unclaimed (never creates an ambiguity).
- Score on seed 7: 0 wrong links of 61k, perfect dedupe, 0 of 2,333 noise records linked. **Caveat:** the rules were tuned on seed 7, then
  re-checked on unseen seed 11 with the same result. The answer key is synthetic; real data will have failure modes this one does not.
  Measure again on a labelled sample of real accounts before trusting it.
*Cost:* name twins with no other evidence are unlinked rather than guessed; alias domains nobody owns (10 tickets) stay unresolved.

## D7. Labels come from Stripe; the CRM only supplies the renewal date (day 2)
CRM opportunities are never reliably closed: 73 of 867 cancellations (8%) sit on a still-open opportunity, and 335 open
opportunities have dates in the past. Outcome = Stripe `subscription.deleted` or a >= 25% seat cut within 3 days of the renewal
date; renewals younger than 14 days are `pending` (late events have not landed). Checked against the answer key: all 867 churns
and 231/232 downgrades right. Renewals more than ~4 months out have no CRM opportunity yet, so scoring projects them
from the last anniversary (flagged as projected).

## D8. Point-in-time features: `known_at`, not event time (day 2)
Every feature uses only facts with `event_time <= S AND known_at <= S`. The leak test hides every row that arrived after a
snapshot and recomputes: features must be identical. It caught a real bug on its first run: an account with no product data
got NaN or 1.0 for the same feature depending on whether data existed *later*. Excluded on purpose: CRM ARR and contract end
(current-state, 25% stale), opportunity stage (the outcome), `cancel_at_period_end` (the customer's own notice: the CSM already
sees it, and a model living on it is not an early-warning system), ticket attributes younger than 14 days (CSAT/status mature late).

## D9. Logistic regression is the production model; boosting is a reported challenger (day 2)
Walk-forward backtest (train on renewals finished before each 6-month fold, never random splits). Pooled AUC 0.647; 0.71 at 4 weeks
out, 0.69 at 8, 0.60 at 24. Boosting is within noise (0.647 vs 0.646 in the last two folds), so the model whose every score is a
sum of readable contributions wins. Calibration error 0.016. In a CSM's top 10 (8 weeks out) 49% are real vs 18% by chance and 34%
for a "tickets + cancellation requests" rule. The data-generating ceiling is 0.81 (knowing hidden health); the observable-feature
logistic in the data validator got 0.67, so we are at that level and have not found a free lunch. Finer 4-week trajectory buckets, log
transforms and an ensemble all moved AUC by < 0.01. First-fold AUC is only 0.61: with one year of history the model is weak.
*Not done:* per-tenant models (tenant AUCs are 0.68-0.71 under one global model), uncertainty intervals, sample reweighting.

## Deliberately not done yet
- Staging/dedupe, entity resolution, features, model, scoring store, agent, UI, tenant isolation (RLS): days 2 to 4.
- No partitioning or incremental-by-watermark loading: 290k rows load in seconds. First thing to change at 10x is to
  partition `raw.product_events` by month and load by `synced_at` watermark instead of re-reading whole files.
