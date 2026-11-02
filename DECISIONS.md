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

## D10. Scoring store, bands and event alerts (day 3)
- Each Monday run scores customers whose next renewal is within 26 weeks (beyond the last CRM opportunity the next anniversary is
  *projected* and flagged). Every row stores run id, model version, the full feature vector, the reasons and the alerts, so any
  number a CSM sees can be recomputed (tested: stored risk == model(stored features)). Re-running the same Monday replaces its rows.
- Bands on calibrated risk: red >= 0.30 (about 13% of accounts, roughly what one CSM can personally work), yellow >= 0.18 (the base rate),
  green below. These are capacity choices, not statistical facts; the VP should set them.
- Reasons: a logistic score decomposes exactly into per-feature contributions (tested to 1e-9), summed into a dozen human concepts,
  each rendered with the account's real numbers.
- **Alerts sit beside the score, not inside it.** A customer writing "considering cancelling" is a statement, not a pattern. Alerts set a
  *floor* on priority from measured outcomes (cancellation ticket: 41.6% churn/downgrade, n=154, floor 0.40) and never alter `risk`.
  Added after the planted-case test showed lost_A was only yellow (0.19) the Monday after its cancellation ticket. Honest reading: that
  ticket arrived 8 weeks and 1 day before the renewal, so 7 weeks is the earliest any system could act on it. Earlier decline was invisible
  (no inbound email for 255 days, 3 of its tickets from personal addresses stayed unlinked). I did NOT retune the model to this account.
- Planted cases (model trained before either renewal, weekly from 26 weeks out): lost_B top-10 from 11 weeks, red from 9;
  lost_A top-10 and red from 7; decoy_declining and decoy_quiet never red. The "at least 8 weeks" target is met for one of two.
- Accounts whose CSM has left the company (55 in the last run) stay in the output, flagged, for routing to a manager.
*Cost:* alerts and the model can disagree, by design; the UI must show both.

## D11. The Monday agent: deterministic, idempotent, never sends (day 4)
- `python -m agent.run --asof <Monday>` scores everyone renewing in 26 weeks, diffs against the previous Monday, queues drafts, and records
  an `agent.run` row. `python -m agent.scheduler --once [--catch-up N]` is the cron entry: it finds the latest Monday whose 06:00 UTC has
  passed and runs it only if no `ok` run exists, so an hourly cron is safe; a file lock stops overlapping invocations. It ran unattended
  here for 5 consecutive Mondays (28 Sep to 26 Oct).
- **Nothing is sent.** The agent writes `pending` drafts into a queue a human approves. Customer emails go only to red accounts whose CSM
  is still employed, to the champion (else decision maker, else any contact) and never to an address that has bounced. Accounts whose CSM
  left get a routing note for a manager instead of a customer email. Customer emails never mention scores, risk or churn (tested).
- **Drafts are templates over stored numbers, not LLM text.** Every figure and date in a draft is rendered from `reasons`, which is saved
  with the draft; a test fails if a draft contains a number that is not in its own reasons, and driver/alert sentences are quoted verbatim
  from `score.account_score`. An LLM rewrite step could be plugged in after rendering and is OFF by default; if added, it must be
  re-verified against `reasons`, because that is exactly where hallucinated numbers would enter.
- Idempotent: the same Monday replaces its own changes, cannot duplicate a draft (unique key + dedupe), and never touches a draft a human
  has decided. All agent writes are ONE transaction (tested: a bad row rolls back the run row, changes and drafts together). A failure is
  recorded in `agent.run` as `failed` with the error and a non-zero exit; a hard kill leaves no row, which the scheduler treats as "not done".
- Dedupe: no new draft for an (account, kind) with a pending/approved/sent draft in the last 14 days. A pending draft older than that is
  **superseded** (marked rejected by `agent:superseded`) and replaced by a fresh one, so the queue never holds two open drafts for the same
  thing and never shows week-old text. Superseded drafts still count for dedupe, so re-running an old Monday reproduces the same queue.
- The first run is a baseline with no changes (nothing to compare to). Risk-jump changes are suppressed when the model changed between the
  two runs, since the numbers are then not comparable. Changes are informational; drafts follow from the current band, not from changes.
*Limits:* (1) an unreviewed queue refreshes every 14 days, so draft volume arrives in bursts (449, 60, 403, 102, 372 in the demo history)
because nobody approved anything; in real use reviewers act within days and the bursts shrink to the genuinely new. (2) The demo history was
scored by the final model (trained through 13 Oct), so the earlier Mondays use a model that has seen later outcomes: it demonstrates the
agent, it is not a backtest (see scoring/PLANTED_CASES.txt for that). (3) Customer emails are generic check-ins: no product-specific
content, no timezone or send-window logic, no unsubscribe handling. (4) A rejected draft by a human does not block the next one.

## D12. Tenant isolation in the database, not only in the application (day 4)
- Two roles. The pipeline role owns the tables and sees every tenant. The web app connects as `fello_app`: not superuser, no BYPASSRLS, no
  CREATEROLE, cannot become the pipeline role (all asserted in a test). After connecting, the app runs `set_config('app.tenant_id', ...)`.
- Row-level security on `score.account_score`, `agent.change`, `agent.draft`: `tenant_id = current_setting('app.tenant_id', true)`. Unset or
  bogus tenant gives zero rows (never "all"), because `current_setting(..., true)` is NULL and NULL never matches.
- `score.run` and `agent.run` have no tenant column (a run spans tenants), so their policy is "visible only if the caller can see at least one
  scored row of that run": the tenant filter does the work and a run row cannot leak. Failed runs with no scores stay operator-only.
- Grants are minimal: SELECT on score/agent tables, and UPDATE only on `agent.draft(status, decided_by, decided_at, edited_body)`. No INSERT,
  DELETE, no access to `core`, `stg`, `raw`, or `er`, and it cannot change `tenant_id`, `body`, or `reasons`, or disable the policies.
- Tested as the real role, by: counting other tenants' rows, guessing ids, an account id that exists in two tenants, joins, UNION, subqueries,
  injection-style tenant names, and trying to UPDATE another tenant's draft. `python -m agent.security` applies everything idempotently.
- **FORCE row-level security is on the `agent.*` tables but not on `score.account_score`:** Postgres refuses `COPY FROM` on a table with forced
  RLS, and the scorer's loader uses COPY. The pipeline role therefore bypasses RLS on `score.*` as an owner; `fello_app` is bound either way.
  Fix when it matters: stage then INSERT in `scoring.score.persist`, then add FORCE.
*Honest limit:* RLS defends against a query that forgets its tenant filter, which is the common failure. It does NOT defend against an app
that is compromised or wrong about who the user is: anything holding the `fello_app` connection can call `set_config` with another tenant.
Real defence in depth is a per-tenant role or a signed, short-lived tenant claim checked in a `SECURITY DEFINER` function, plus the
app's own authorisation. The model is also one global model across tenants (not isolated); features and scores are.

## Deliberately not done yet
- Staging/dedupe, entity resolution, features, model, scoring store, agent, UI, tenant isolation (RLS): days 2 to 4.
- No partitioning or incremental-by-watermark loading: 290k rows load in seconds. First thing to change at 10x is to
  partition `raw.product_events` by month and load by `synced_at` watermark instead of re-reading whole files.

## D13. The CSM interface: standard library, server-rendered, no send path (day 5)
- `ui/` is a stdlib HTTP server rendering HTML: no framework to install (PyPI is blocked here, and there is nothing a CSM needs from one).
  Database access sits behind one function (`ui/db.py: query(sql, params, tenant_id)`); a psql driver locally, a psycopg driver deployed.
- Tenant safety in three layers: the session decides the tenant (never the URL or form), every SQL statement filters on it anyway, and
  the app connects as `fello_app` where Postgres row-level security enforces it. The restricted-role test caught a real bug the owner
  role had hidden: the "latest run" lookup carried no tenant, so under RLS every page said "no scores yet".
- A CSM sees only their own accounts and drafts (404, not 403, for anyone else's). Managers see their whole tenant.
- The app cannot send email: approving a draft only changes its status (tested by scanning the code for any mail/network client).
  The agent drafts, a person decides, a person sends.
- Alerts are shown first and the row number is the *priority* (model score, lifted by any alert), so number, band and order agree;
  the account page shows both numbers and says why they differ.
- Drafts replaced by a newer one for the same account and kind are hidden, with a count. The agent should expire them itself.
*Cost:* server-rendered pages reload on every action; fine for a weekly list of ~50 accounts, wrong for a live dashboard.
