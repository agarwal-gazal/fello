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
| Monday agent (`agent.*`), approval queue | **Verify before submitting.** Section 8 is written from the agreed contract. |
| Tenant isolation with Postgres row-level security | **Verify before submitting.** Section 9. |
| CSM interface (`ui.server`) | **Verify before submitting.** Section 10. |
| Live deployment and demo logins | **Verify before submitting.** Section 11 has placeholders. |
| Loom walkthrough | Not recorded yet. |

Anything in sections 8 to 11 that does not match what is actually running should be fixed or deleted before this goes out.

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

Timings are from a 2-core sandbox at the data size above. `make test` runs the whole suite (89 tests when I last ran it, all passing, about 4 minutes, including the agent, security and UI tests). Several test classes build their own small dataset end to end.

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

## 8. The Monday agent (verify before submitting)

Contract, as agreed for this build:

- A scheduled job runs with no human present. It rescores everyone as of the Monday, compares with the previous run, and writes what changed (new red, moved up, new alert, newly silent).
- For each account that changed, it drafts a short note for the CSM (why this account, with the real numbers) and a suggested outreach email.
- Tables: `agent.run` (one row per execution), `agent.change` (what moved), `agent.draft` (the notes and emails).
- **Drafts are only ever created as `pending`. A person approves or rejects. The agent has no code path that sends mail.**
- Commands: `python -m agent.run --asof 2026-10-26` for one run, `python -m agent.scheduler --once` for one scheduler tick.
- Re-running the same Monday must replace its rows, not duplicate them (the scoring store already does this by run id).
- If a run fails part way, the earlier run stays the one the UI shows, and the failure is recorded in `agent.run`.

Please check against the code: the table and column names, whether drafts are templated or LLM-written (the brief says an LLM may only phrase computed facts, never produce a score), and what exactly happens on failure.

## 9. Tenant isolation with row-level security (verify before submitting)

Contract: the application connects as role `fello_app`, which owns nothing and bypasses nothing. Every request runs `set local app.tenant_id = '<tenant>'`. Row-level security policies on tenant tables allow only rows where `tenant_id = current_setting('app.tenant_id')`. A test connects as `fello_app` for tenant A and asserts that every query against tenant B's rows returns nothing, including joins and the approval queue.

Please check: which tables have policies, that no table was missed, and that the test fails if a policy is removed.

## 10. The CSM interface (verify before submitting)

`python -m ui.server --port 8080`. Screens: login, **My week** (the CSM's ranked list with band, reasons, alerts and data notes), **Account** page (score, drivers, the tickets, emails and events behind them), **approval queue** (approve or reject drafts), **manager view** (including accounts whose CSM has left). It is a working interface, not a polished one. The brief said so up front.

## 11. Live system (verify before submitting)

- URL: `TODO`
- Demo logins for two tenants: `TODO`
- Scheduler: `TODO: where it runs and when it last ran unattended`

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
8. **The scheduler is a single process.** No leader election, no queue. A second copy running at the same time would be harmless for scoring (rows are replaced by run id) but could write duplicate drafts unless the agent run is made idempotent too. Verify against the agent code.
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
agent/        Monday agent            (verify, section 8)
ui/           CSM interface           (verify, section 10)
tests/        unittest suites
docs/         architecture diagram, Loom script, submission checklist
deck/         short slide deck
```
