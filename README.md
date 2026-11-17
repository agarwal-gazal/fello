# Weekly CSM priority list: account health from five messy systems

Take-home for the Staff Engineer, GTM AI Team role at Fello. Author: Gazal Agarwal.

The request was a red/yellow/green health dashboard plus automated outreach emails. I read the real need as something else: a CSM with about 120 accounts cannot tell which five to call this week, or why. So I built a pipeline that ends in a short, ranked, explained worklist per CSM, and a Monday job that drafts the follow-up into an approval queue. A colour is kept only as a summary of the ranking.

One-page brief (written before the code): <https://claude.ai/artifact/HdLmEZf2Exxa1zUntSNcMV>. Decision log, written as each call was made: [`DECISIONS.md`](DECISIONS.md) (D1 to D10).

## STATUS: read this first

| Part | State |
|---|---|
| Synthetic data (5,000 accounts, 201,987 event rows, 3 tenants), validator | Built, tested |
| Raw loader, staging, entity resolution, core layer | Built, tested |
| Point-in-time features with a leak test | Built, tested |
| Model, walk-forward backtest, scoring store, reasons, event alerts | Built, tested |
| Planted-case backtest (the two lost renewals) | Built, results below |
| Monday agent (`agent.*`), approval queue | Built, tested (18 tests). Section 8. |
| Tenant isolation with Postgres row-level security | Built, tested (11 tests). Section 9 lists what is not covered. |
| CSM interface (`ui.server`) | Built, tested (18 tests), screenshots in `ui/screens/`. Section 10. |
| Live deployment | **Not deployed.** Runs locally. Deploy files and steps are in `deploy/`. Section 11. |
| Loom walkthrough | Not recorded yet. |


## 1. Results, stated plainly

All numbers come from `model/BACKTEST.txt`, `resolve/EVALUATION.txt` and `scoring/PLANTED_CASES.txt`, reproducible with the Makefile targets below.

**Model.** Walk-forward backtest: for each 6-month fold, train on renewals that finished before it, test on the next six months. Never a random split.

- Pooled AUC 0.647 over three folds (0.612, 0.652, 0.665). The first fold has only about a year of history and is weak.
- By how far before renewal the snapshot is taken: AUC 0.710 at 4 weeks, 0.689 at 8, 0.664 at 12, 0.642 at 16, 0.597 at 24.
- Calibration error 0.016. In the 0.30 to 0.40 bucket the mean prediction is 0.346 and the actual rate 0.361.
- The product metric: of each CSM's 10 riskiest accounts renewing in the window (8 weeks out), 48.9% really churned or downgraded. Chance is 18.4%. A rule of "tickets in 90 days plus cancellation tickets" gets 34.3%.
- Those 10 per CSM catch 26.0% of all at-risk renewals. The other three quarters are not in anyone's top 10. This is a ranking aid, not a detector.
- The ceiling on this data is AUC 0.81, and that needs the hidden true health, which no pipeline sees. A plain logistic regression on eight simple features reached 0.67 in the data validator. I am at that level. Finer trajectory features, log transforms and a boosted ensemble each moved AUC by less than 0.01, so I stopped.
- Weakest segment: SMB (AUC 0.651 at 8 weeks). Strongest: Enterprise (0.774, n=347, small).

**The two lost renewals** (model trained only on renewals finished before 2026-04-13, which is before either loss; scored every Monday from 26 weeks out, ranked among the CSM's other accounts renewing in the next 26 weeks):

| Account | What was hidden in the mess | First stable alert |
|---|---|---|
| `lost_B` | Champion left, emails to them bounce, workspace name unrelated to the CRM name, CRM website blank | In the CSM's top 10 from 11 weeks out, red from 9 weeks |
| `lost_A` | None of its tickets carry an organisation, Stripe has no Salesforce id | In the top 10 and red from 7 weeks out |
| `decoy_declining_renewed` | Same decline as `lost_A`, then saved | Never red; ranks 34 to 48 of its CSM's list |
| `decoy_quiet_renewed` | Almost no data anywhere, renews anyway | Never red; ranks 29 to 45 |

The target was at least 8 weeks. I met it for one of the two. Why `lost_A` is 7 and not more:

- Its "Considering cancelling" ticket was filed 8 weeks and 1 day before the renewal and reached the warehouse two days later. The first Monday that could act on it is 7 weeks out. No system could do better on that signal.
- The earlier decline was invisible: no inbound email for 255 days, and 3 of its 14 tickets came from personal addresses, which entity resolution deliberately leaves unlinked.
- On its own the model gave that Monday only 0.19 risk (yellow). Cancellation tickets in this data lead to churn or a downgrade 41.6% of the time (n=154), against 18.2% overall, so 0.19 is not absurd. I did not retune the model to this one account. I added event alerts instead (section 6).

**Entity resolution** (scored against the generator's answer key, which the pipeline never reads):

- Salesforce de-duplication: 5,127 records resolve to exactly 5,000 accounts. Pairwise precision and recall 1.0. 29 pairs of different companies with near-identical names, none merged.
- 0 wrong links out of about 61,000. 0 of 2,333 test-mode and internal records linked.
- Coverage: Stripe 100%, Zendesk orgs 100%, workspaces 100%, Gmail 99.1%, Zendesk tickets 95.6%.
- The gap: 551 tickets, 242 emails and 1 Stripe customer come from personal addresses (gmail.com and the like) with no known person behind them. They stay unlinked. 10 more tickets use alias domains that no account owns. I would rather leave 4% of tickets out than put them on the wrong account.

**Caveat that matters.** I wrote the generator, the planted cases and the answer key, then tuned the resolution rules against them. I re-checked on a second, unseen seed (seed 11) with the same result, but the key is still synthetic. Real data will have failure modes this one does not. Before trusting the resolution on real data, measure it on a labelled sample of real accounts. The modelling numbers are bounded by how hard I made the data. Treat them as evidence that the pipeline is sound and honest about leakage, not as a forecast of real-world accuracy.

## 2. Architecture

```
raw.*  ->  stg.*  ->  er.*  ->  core.*  ->  feat.training  ->  model  ->  score.*  ->  agent.*  ->  ui
```

![architecture](docs/architecture.png)

| Layer | What it does | Code |
|---|---|---|
| `raw` | Verbatim JSON, one table per source entity. Identity is `(tenant_id, md5(line))`, so reloading is a no-op. `synced_at` (when the warehouse learned of it) is required. | `ingest/load_raw.py`, `ingest/schema.sql` |
| `stg` | Typed, deduplicated: Stripe webhook retries, Zendesk ticket versions, re-sent product events. Test-mode customers and internal QA workspaces flagged, naive timestamps and future-dated events flagged. | `ingest/staging.sql` |
| `er` | One account id across Salesforce, Stripe, Zendesk, product and Gmail. Every link stores method, confidence, conflict flag, and a reason when unlinked. | `resolve/` |
| `core` | Each fact keyed by account, carrying both event time and `known_at`. Renewal outcomes from Stripe. | `core/core.sql` |
| `feat` | Point-in-time features: only facts with `event_time <= S and known_at <= S`. | `features/` |
| model | Logistic regression on sign-log counts. Boosting kept as a reported challenger. | `model/` |
| `score` | Per account per Monday: calibrated risk, priority, band, reasons, alerts, and the full feature vector. | `scoring/` |
| `agent`, `ui` | See sections 8 and 10. | `agent/`, `ui/` |

Data stays in Postgres 16. The loader shells out to `psql \copy` instead of using a Python driver, because the build sandbox could not reach PyPI (D5). Deploy-time `pip install` of a driver is straightforward.

## 3. How to run it

Prerequisites: Postgres 16, Python 3.11+, `numpy`, `pandas`, `scikit-learn` (see `requirements.txt`). Create a database and set `DATABASE_URL`.

```
make data       # generate synthetic data, deterministic, about 40 s
make validate   # evidence that the data is realistic and carries signal
make load       # raw files -> raw.*  (idempotent, about 10 s)
make stage      # raw -> stg.*  (about 14 s)
make resolve    # entity resolution -> er.*  (about 6 s)
make evaluate   # score resolution against the hidden answer key
make core       # core.* and renewal labels  (about 15 s)
make features   # point-in-time training snapshots  (about 40 to 50 s)
make train      # fit and backtest -> model/BACKTEST.txt, model/artifacts/model.pkl
make score ASOF=2026-10-26    # score every account renewing within 26 weeks  (about 30 s)
make planted    # would the system have caught the two lost renewals?
make test       # all tests; needs a scratch DB at postgresql://fello:fello@localhost/fello_test
```

Local setup on a Mac, matching CI (Postgres 16, Python 3.13, the pinned versions in `deploy/requirements.txt`, which the pickled model needs):

```
brew install postgresql@16 python@3.13 && brew services start postgresql@16
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"      # the code calls the psql command
psql postgres -c "create role fello login superuser password 'fello'"   # superuser: the tests create the fello_app role
createdb -O fello fello_health && createdb -O fello fello_test
python3.13 -m venv .venv && .venv/bin/pip install -r deploy/requirements.txt
source .venv/bin/activate && make test
```

Timings are from a 2-core sandbox at the data size above. `make test` runs the whole suite (93 tests when I last ran it, all passing, about 2 minutes on an Apple-silicon Mac, including the agent, security and UI tests). Several test classes build their own small dataset end to end.

Agent, scheduler and UI commands: sections 8 and 10.

## 4. Decisions I would defend

The full reasoning is in `DECISIONS.md`. The ones a reviewer is most likely to question:

1. **Validate the synthetic data before building on it (D1).** My first generator had almost no learnable signal: logistic AUC 0.62 against a 0.81 ceiling. A model trained on that would have "worked" by accident. I steepened the couplings and re-measured. The ceiling number is how I keep myself honest about a world I built.
2. **Raw is verbatim and append-only; deduplication lives in staging (D4).** Source-side duplicates carry different `_synced_at` values and are kept in raw. Dedupe is a business rule and belongs where it can be seen and tested.
3. **Wrong is worse than missing (D6).** A bad link feeds another account's features. An unlinked ticket only lowers coverage. A personal email address is a person, not a company, so such matches are capped at 0.75 confidence and company evidence overrules them.
4. **Labels from Stripe, not from the CRM (D7).** 73 of 867 cancellations (8%) sit on a CRM opportunity that was never closed, and 335 open opportunities have dates in the past. The CRM supplies only the renewal date. Checked against the answer key: all 867 churns and 231 of 232 downgrades are right.
5. **Features go by arrival time, not event time (D8).** The leak test hides every row that arrived after a snapshot and recomputes the features. They must be identical. On its first run it caught a real bug: an account with no product data got NaN or 1.0 for the same feature depending on whether data appeared later.
6. **Features I left out on purpose (D8).** CRM ARR and contract end date (current-state snapshots, 25% stale), opportunity stage (it is the outcome), Stripe `cancel_at_period_end` (the customer's own notice; the CSM already sees it, and a model living on it is not an early-warning system).
7. **Logistic regression in production (D9).** Within noise of boosting (0.647 vs 0.646 in the last two folds), and every score is a sum of readable contributions that a CSM can be shown.
8. **Alerts beside the score, not inside it (D10).** See section 6.
9. **Drafts only, never auto-send (brief, section 8).** An at-risk account is where a CSM's judgement counts most, and a false alarm can cost a relationship.

## 5. Traceability: everything a CSM sees comes from something the system computed

- Each `score.account_score` row stores: run id, model version (hash), the full feature vector, the reasons, and the alerts.
- The stored risk can be recomputed from the stored features with the stored model. This is a test, not a claim (`tests/test_scoring.py`).
- A score decomposes exactly into per-feature contributions (tested to 1e-9). They are summed into about a dozen concepts such as "usage trend" or "support load", and each reason is rendered with the account's real numbers: "7 tickets in the last 90 days (2 in the last 30)".
- From a reason to the evidence: `core.ticket`, `core.email`, `core.product_event`, `core.invoice` and `core.billing_event` all carry `account_id` and the link method and confidence. From there to the source row by `(tenant_id, md5)` in `raw`.
- The language that explains a score is built from those computed facts. The model ranks; nothing writes a score.

## 6. Bands, alerts and priority

- Bands on calibrated risk: red at 0.30 or more (about 13% of accounts, roughly what one CSM can personally work), yellow at 0.18 or more (the base rate), green below. **These two thresholds are my capacity guess, not a statistical fact. The VP should set them.**
- Accounts with no signal in any source, or silent for 90 days, get a data note ("no data in any source", "silent for 90 days"), because quiet is not the same as healthy. About 380 of the 1,632 scored accounts carry one.
- Alerts are explicit events that set a floor on priority and never change the model's risk. A customer writing "considering cancelling" is a statement, not a pattern. The floor of 0.40 comes from the measured rate (41.6%, n=154). A bounced email to the champion contact sets only a yellow floor and is labelled as thin evidence: it rests on 5 historical cases.
- Alerts and the model can disagree. That is by design; the UI shows both.
- 55 accounts in the last run belong to a CSM who has left. They stay in the output, flagged, so they can be routed to a manager rather than silently dropped.

## 7. Multi-tenancy

- `tenant_id` is on every table and in every key. Ids are unique per tenant only: 3,053 Zendesk ticket ids collide across tenants in the data, and keying by id alone would merge them.
- Entity resolution runs per tenant. Domains, names and people are never matched across tenants.
- One global model, with no tenant identity as an input. Features are mostly each account's own trend against its own baseline, which transfers across tenants. Per-tenant AUC at 8 weeks out under the one model: Beacon 0.708, Lumen 0.684, Parcelwise 0.683. Mean predicted risk is close to the actual rate in each (0.167 vs 0.159, 0.176 vs 0.175, 0.189 vs 0.198).
- Whether customers' data may be pooled into one model is a contractual question, not a technical one. It is on my list of things I need from the business.
- Isolation at the database: see section 9.

## 8. The Monday agent

`python -m agent.run --asof 2026-10-26` does one Monday. `python -m agent.scheduler --once` is the cron-safe entry point.

What a run does, in order:

1. Scores everyone renewing within 26 weeks, as of that Monday, and stores the result (`scoring.score`).
2. Compares with the previous stored run and writes `agent.change` rows: `entered_red`, `left_red`, `new_alert`, `risk_jump` (up by 0.10 or more, suppressed if the model changed between the runs), `csm_departed`. The first run is a baseline and has no changes.
3. Drafts text into `agent.draft`. Two kinds: `csm_note` (why this account, with the real numbers) and `customer_email`. Customer emails are only drafted for red accounts whose CSM still works here, go to a contact from the CRM, never to an address that has bounced, and never mention scores or risk. An at-risk account whose CSM has left gets a routing note instead.
4. Records the run in `agent.run`.

Facts worth knowing:

- **Drafts are templates, not an LLM.** Every sentence comes from the stored drivers and alerts of that score row. A test fails if a draft contains a number or date that is not in its stored `reasons`. An LLM rewrite step could be added later, but it would have to be re-checked against `reasons`.
- **Drafts are always created `pending`.** A person approves or rejects in the web app. No code in the agent or the app sends email, and a test scans for it.
- **Re-running a Monday is safe.** Rows are replaced by run id, a unique key stops duplicate drafts, and drafts a person already decided are never touched. A draft for the same account and kind inside 14 days is not repeated. An unreviewed draft older than that is marked rejected by `agent:superseded` and replaced.
- **The writes to `agent.*` happen in one transaction.** A test checks that a write failing halfway leaves nothing behind.
- **Failures are loud.** An exception is stored in `agent.run` as `failed` with the error, the process exits non-zero, and a later re-run repairs it.
- **A failed Monday is not shown as current.** Scoring is saved before the agent's own transaction. If the agent fails in between, those scores exist, but the web app shows only the newest Monday whose `agent.run` status is `ok` (tested). A re-run repairs it.
- **The scheduler** works out the latest Monday and runs it only if no `ok` run exists, takes a local file lock, and `--catch-up N` runs missed Mondays oldest first. It uses the data export time as "now" unless `--now wall` is passed, because the data is a frozen export.
- **Draft volume comes in bursts.** In the demo history, nobody approved anything, so the queue reached 449 drafts in one week. There is no cap per CSM yet.
- The earlier Mondays in the demo history were scored with the final model, which has seen later outcomes. That history shows the agent working, not a backtest.

## 9. Tenant isolation with row-level security

The web app connects as `fello_app`, a role that owns nothing and cannot bypass security. Every request sets `app.tenant_id` for its transaction (locally, once per `psql` call). Policies allow a row only when its `tenant_id` equals that setting. Policies exist on `score.account_score`, `score.run`, `agent.run`, `agent.change` and `agent.draft`. The pipeline's owner role has its own policy and sees everything. `fello_app` can read the score tables and agent tables, and can update only `status`, `decided_by`, `decided_at` and `edited_body` on a draft. It cannot read `raw`, `stg`, `er` or `core`.

Tests (`tests/test_isolation.py`, 11) connect as `fello_app` and check, for each tenant: only its own rows are visible in each table, guessing another tenant's ids returns nothing, joins, unions and subqueries do not widen the view, with no tenant or a bogus one it sees nothing, it cannot update another tenant's draft, and it cannot insert, delete, disable security or drop a policy.

What this does **not** cover:

- Row-level security is enabled but not *forced* on `score.account_score` and `score.run`. The scoring store loads with `COPY`, which Postgres refuses on a forced table. The app role is not the owner, so it is still filtered. Forcing it needs the loader to stage and then insert.
- `score.run` and `agent.run` have no `tenant_id` column. A run is visible to a tenant only if that tenant can see at least one of its scored rows.
- A compromised app server holds the `fello_app` connection and could set another tenant's id. This isolates tenants from bugs in queries, not from a stolen app.
- The web app also filters by tenant in its own SQL, as a second layer.

## 10. The CSM interface

`python -m ui.server --port 8080` (health check at `/healthz`). Standard library only, server-rendered, no JavaScript framework. Login with a password hashed with scrypt, a signed cookie, CSRF tokens on every POST and a rate limit on login. Two roles:

- **CSM:** *My week* lists their accounts renewing within 26 weeks, ranked by priority, with band, reasons, alerts and data notes, plus what changed since last week. An *account page* shows the risk in plain terms, the drivers and protective factors, alerts with their evidence, data-quality notes (low data, silent account, projected renewal date, departed CSM), a risk-over-time line, and a collapsible "how this was computed" panel with the run id, model version and stored feature vector. The *approval queue* lists drafts with the facts they cite, and lets the CSM approve, reject or edit. Approving only changes the status.
- **Manager:** a team view with counts by band per CSM and the accounts whose CSM has left.

The account page shows the stored score inputs. It does not list the underlying tickets, emails or events. It is a working interface, not a polished one.

## 11. Live system

**Not deployed.** The whole system runs locally (`make` targets), and the same pipeline runs in GitHub Actions. What exists:

- `deploy/DEPLOY.md` has step-by-step instructions for a Neon database and a Render web service, with a `Dockerfile`, `render.yaml` and `bootstrap.sh` that builds everything inside an empty database. I could not test the Docker build, Render, Neon or the `psycopg` code path here. `bootstrap.sh` was tested against a local database at 5% scale (41 s).
- `.github/workflows/ci.yml` runs the tests on each push. `.github/workflows/weekly-agent.yml` builds a smaller system in a throwaway database each Monday and runs the scheduler. It shows unattended operation but keeps no history between runs. I wrote both and they are untested until GitHub runs them.
- The scheduler has run unattended locally for five consecutive Mondays (28 Sep to 26 Oct 2026), all `ok`. Calling it again does nothing.
- Demo logins come from `python -m ui.seed`: `csm.lumen`, `csm.parcel`, `manager.lumen`, `manager.parcel`. The password comes from `DEMO_PASSWORD`; change it before sharing a URL.

## 12. What I skipped, on purpose

- **Per-tenant models and thresholds.** One global model; tenant AUCs are 0.68 to 0.71. Needs more tenants and more history to justify.
- **Uncertainty intervals** on the risk number.
- **Learning from the CSM's response.** Approved and rejected drafts are stored but not fed back. There is no measurement yet of whether an outreach changed an outcome, and without a holdout there cannot be.
- **Monitoring and drift checks** on feature distributions and calibration.
- **Real-time scoring.** Weekly is what the VP described, and the backtest is built around it.
- **Involuntary churn, win-backs, multi-currency, tax, GDPR deletions, email bodies, schema drift between exports, multi-product subscriptions.** Listed in `data_gen/DATA_DICTIONARY.md` section 9. None is needed to exercise the design decisions, all would matter in production.
- **An orchestrator, dbt, or incremental loads.** Every layer is rebuilt from scratch on each run (see below).
- **Product telemetry at full volume.** The event stream is a sample (D2): counts are relative activity, never absolute. `--usage-scale 5` makes it denser with the same logic.

## 13. What breaks at 10x

Today: 290,245 rows across all sources (201,987 event rows, 129,792 of them product events), 82 MB of raw JSON, 5,000 accounts, 3 tenants. Measured on a 2-core sandbox: load 10 s, staging 14 s, resolution 6 s, core 15 s, features about 40 to 50 s, scoring about 30 s. Everything below at 10x is my estimate, not a measurement.

In the order I expect things to fail:

1. **`features.compute` is a per-account Python loop that builds events-by-snapshots boolean matrices for every window.** Cost grows with events per account times snapshots, not just with row count. At 1x an account has about 25 product events (the stream is sampled). With real, unsampled telemetry a large account has tens of thousands, and the matrices stop fitting in memory long before the totals look large. Fix: aggregate in SQL to account-day counts (keeping the arrival day as well, so `known_at` filtering still works), and compute windows from those.
2. **Scoring loads every fact table into pandas on each run** (`load_tables`). Fine at 125k product events, not at real volume. Same fix: aggregate in the database, load only what the scored accounts need.
3. **Everything is rebuilt from scratch.** At roughly 80 s end to end today, linear scaling would give about 13 minutes at 10x. That is tolerable weekly and unacceptable for fixing a bad load. The change is incremental builds by a `synced_at` watermark, and partitioning `raw.product_events` by month (D5).
4. **Loader and reads go through `psql` and strings.** `\copy` is fast, but every read into pandas is a CSV round trip with everything parsed as text. A real driver and typed reads would be the first change.
5. **Entity resolution** keeps domain and name indexes in memory per tenant and does fuzzy name matching with trigram blocking in Python. It scales to a larger tenant, but ambiguity grows with size: more name twins, more shared domains, more personal addresses. The account id is the earliest-created Salesforce record, so a newly arriving earlier duplicate could change an id. I would introduce a stable surrogate id before putting this near real downstream references.
6. **One global model** is a defensible choice at three tenants. At a hundred, tenants with different products and very different base rates make per-tenant recalibration, at least, necessary. A new tenant has no history, so cold-start handling matters more than the algorithm.
7. **Row-level security** adds a predicate to every query. It is cheap when indexes lead with `tenant_id` (the core indexes do), but needs a connection pool that sets the tenant per transaction, never per session.
8. **The scheduler is a single process.** No leader election, no queue. The lock is a local file, so two machines would not see each other. A second copy of a run is harmless: rows are replaced by run id and a unique key stops duplicate drafts. A real deployment would use a database advisory lock or a queue.
9. **Human capacity.** More accounts per CSM means longer lists, not better ones. The band thresholds, not the pipeline, are what would actually need rethinking.

## 14. Tests

`make test`. Beyond unit tests of names and edit distance, the ones that carry weight:

- Raw load is idempotent and survives backslashes in JSON.
- Staging collapses retries and ticket versions, keeps the first arrival as `known_at`, and flags noise exactly as the answer key does.
- Entity resolution: no noise is linked, precision at least 99.5% per entity, no two real companies merged, rerun is deterministic.
- Features: hiding every row that arrived after a snapshot changes no feature. A backfilled email is invisible until it arrives. Windows are half-open.
- Scoring: a customer that already left is not scored, ranks are a permutation per CSM, the stored risk is reproducible from the stored features, drivers sum to the score, persistence is idempotent, and an alert respects knowledge time and never alters the model's risk.

## Repository map

```
data_gen/     generator, validator, DATA_DICTIONARY.md, VALIDATION_REPORT.txt
ingest/       raw loader, staging SQL
resolve/      entity resolution and its evaluation (EVALUATION.txt)
core/         account-keyed facts and renewal labels
features/     point-in-time features
model/        training, backtest (BACKTEST.txt), explanations
scoring/      weekly scoring, alerts, planted-case backtest (PLANTED_CASES.txt)
agent/        Monday agent, approval-queue tables, row-level security
ui/           CSM interface
tests/        unittest suites
docs/         architecture diagram
deck/         short slide deck
deploy/       Dockerfile, Render and GitHub Actions config, DEPLOY.md, bootstrap.sh
```
