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

## Deliberately not done yet
- Staging/dedupe, entity resolution, features, model, scoring store, agent, UI, tenant isolation (RLS): days 2 to 4.
- No partitioning or incremental-by-watermark loading: 290k rows load in seconds. First thing to change at 10x is to
  partition `raw.product_events` by month and load by `synced_at` watermark instead of re-reading whole files.
