# Synthetic data: what it is, how it was made, what is wrong with it on purpose

Generate: `python -m data_gen.generate --out data --seed 7` (about 40 s, deterministic for a given seed and numpy version).
Check it: `python -m data_gen.validate --data data` (writes nothing; prints the evidence quoted below).

## 1. Shape

| | |
|---|---|
| Customer organisations (tenants) | 3: `tnt_lumen` (2,300 accounts), `tnt_parcel` (1,700), `tnt_beacon` (1,000) |
| Accounts | 5,000 total; 3,799 active, 1,201 churned |
| History | 2023-10-30 to 2026-10-26 (157 weekly buckets); export cutoff `2026-10-27T00:00Z` |
| **Event rows** (product, Gmail, Zendesk tickets, Stripe invoices + events) | **201,987** |
| All rows incl. CRM entities and reference tables | 290,245 |
| CSMs | ~118 live accounts each (median); 8 % of CRM users are departed staff who still own ~155 accounts |

Event rows by source: product 129,792 · Gmail 29,730 · Zendesk 13,112 · Stripe invoices 17,065 · Stripe events 12,288.

Each tenant is a separate B2B SaaS company with its own Salesforce, Stripe, Zendesk, product and Gmail. **Tenants differ on purpose**
(share of renewals ending in churn or downgrade: Lumen 17.7 %, Parcelwise 19.9 %, Beacon 15.6 %; ticket and usage intensity; how well Stripe links to Salesforce: 62 % / 36 % / 78 %; product feature names),
so a single global model is an assumption to defend, not a given.

## 2. Layout

```
data/raw/<tenant_id>/<source>/<entity>.jsonl     the only thing the pipeline may read
data/ground_truth/                               hidden truth, for EVALUATION ONLY
data/manifest.json                               seed, window, row counts
```

Every raw record carries **`_synced_at`**: when it reached the warehouse (like Fivetran's `_fivetran_synced`). It is *knowledge time*.
Event time (`created_at`, `timestamp`, `internalDate`...) is *world time*. Point-in-time features must filter on `_synced_at`.
Files are written **in shuffled order**; nothing is sorted.

## 3. Sources

**Salesforce** (a current-state snapshot: no history).
`accounts` (Id, Name, Website, Industry, NumberOfEmployees, Segment__c, OwnerId, Type, Contract_End_Date__c, ARR__c, CreatedDate, LastModifiedDate),
`contacts` (AccountId, Email, Title, Is_Champion__c, Is_Decision_Maker__c), `opportunities` (Type Renewal/Upsell, StageName, CloseDate, Amount, IsClosed, IsWon), `users` (CSMs).
Opportunity stage is the **final** stage. Using it as a feature "as of" an earlier date leaks the outcome.

**Stripe** (snapshot plus webhook stream).
`customers` (name, email, metadata.salesforce_id sometimes), `subscriptions` (current state: quantity, plan.interval, current_period_*, cancel_at_period_end, status),
`invoices` (amount_due, status paid/open/uncollectible, attempt_count), `events` (`customer.subscription.updated/deleted/created`, `invoice.payment_failed`, with `previous_attributes`).
History starts at the window start. Annual contracts paid monthly look like monthly subscriptions: **the renewal date is not in Stripe for those**.
The label (non-renewal or material downgrade) comes from here: `subscription.deleted`, or a `subscription.updated` with a quantity drop of at least 25 % at an anniversary.
`cancel_at_period_end=true` appears 7 to 60 days before the end date: a strong but *late* signal; a model living on it is not an early-warning system.

**Zendesk.** `organizations` (name, domain_names, external_id), `tickets` (requester_email, organization_id, subject, description, priority, tags, status, created/solved, first_reply_minutes, satisfaction_rating).

**Product analytics.** `events` (event, user_id or anonymous_id, workspace_id, timestamp, properties), `workspaces` (name, owner_email, crm_account_id sometimes), `users` (email, role, deactivated_at).
**The event stream is a sample** (deliberate: it keeps the whole dataset near 200k rows). Read counts as relative activity, never absolute. Large accounts have far more events than small ones.
`--usage-scale 5` produces a denser stream with identical logic.

**Gmail** (CSM mailboxes, native Gmail API shape).
`messages`: id, threadId, labelIds (`SENT`/`INBOX`; direction must be derived), snippet, internalDate (ms), `payload.headers` (From/To/Subject), `_mailbox`.
Snippets only, no bodies (privacy).

## 4. Rules that are easy to get wrong

1. **IDs are unique per tenant, not globally.** Zendesk ticket and organisation ids collide across tenants (3,053 duplicate ticket ids). Key by `(tenant_id, id)`.
2. **Zendesk tickets can appear twice** (an older "open" version and the final one): keep the latest `updated_at`. **Stripe webhooks and product events duplicate** too.
3. **No account has a single key.** Salesforce Id, Stripe customer, Zendesk org, product workspace and email domains must be reconciled (see section 6).
4. **Timestamps lie sometimes**: 3.9 % of product timestamps are local time with no zone, 0.9 % are future-dated relative to arrival.
5. **Silence has two meanings.** "No events" can mean *unhealthy* or *never instrumented / never connected*. See `missing_sources` in the truth file and the mailbox backfill below.
6. **Tenant staff appear as users**: an internal QA workspace (2 % of product volume) and 50 test-mode Stripe customers. Neither is a customer.

## 5. The mess, with measured rates (from `validate.py`)

| Hazard | Measured |
|---|---|
| Duplicate Salesforce accounts (contacts/opps split between the two) | 2.5 % of accounts |
| Salesforce website missing | 9.2 % |
| Stripe customers carrying a Salesforce id | Lumen 62 %, Parcelwise 36 %, Beacon 78 % (a few point at the *duplicate*) |
| Stripe ↔ Salesforce by exact name | only 61 % (spelling variants: Inc./LLC, case, "The", typos, abbreviations) |
| Zendesk orgs linked to Salesforce by external_id | 40 % |
| Zendesk tickets with no organisation | 38.1 % |
| Product workspaces carrying a CRM id | 35 % |
| Alias email domains (parent company, rebrand) | 4.9 % of accounts |
| **Near-identical names that are different companies** | 29 pairs (name-only matching merges strangers) |
| Product events arriving >1 day late | 8.6 % (1.9 % are 15 to 90 day backfills) |
| Product events visible at 06:00 the following Monday | 94.5 % mean (worst week 92.3 %) |
| Gmail early-window mail that landed >14 days after sending | 33 % (mailbox connected late, bulk backfill). **Training on event time instead of `_synced_at` leaks the future here.** |
| Whole source missing for an account | product 5.1 %, Zendesk 8.1 %; Gmail missing for CSMs who never connected |
| Accounts with <5 activity rows over their whole life | 29.5 % (7.7 % have none) |
| Cold-start accounts (<100 days old, no renewal history) | 12.1 % |
| Stale CRM: ARR (25 %), contract end date (8 %), churned accounts still typed "Customer" (10 %), ownership by departed CSMs | see truth file |
| Gmail noise (internal, newsletters), out-of-office auto-replies, bounces after a champion leaves | present |
| Shared incidents: 3 outage weeks (tickets x3.2, usage x0.6) and 4 release weeks per tenant, plus holiday dips | confounders that are **not** account health |

## 6. How the data was made (why the signals are believable)

Each account gets a **hidden weekly health curve** from one of eight archetypes: `steady_healthy` 36 %, `steady_ok` 17 %, `slow_decline` 12 %, `sudden_drop` 9 % (champion leaves),
`recovering` 6 %, `volatile` 7 %, `quiet_loyal` 8 % (healthy but generates little data), `expanding` 5 %.
Every source is generated **from that curve with its own lag and noise**, so signals do not move in lock-step. Examples: ticket volume rises as health falls;
the customer's email reply rate and speed fall; usage drops; payment failures rise; a departed champion's address bounces.

At each anniversary, churn is a logistic function of mean health over the 17 to 4 weeks before renewal, plus segment and first-renewal effects, plus a **2 % health-independent
churn** (acquisition, budget cut). Outcomes: renewed 71.7 %, expanded 10.3 %, **churned 14.1 %, downgraded 3.9 %**. Pending renewals have a hidden outcome in the truth file only.

**What this implies for modelling** (measured with the validator):

| | AUC on a time-split test |
|---|---|
| Knowing the hidden health exactly (ceiling) | 0.813 |
| Plain logistic regression on 8 simple as-of features | 0.666 (0.698 on mid-market + enterprise) |
| Best single raw feature | ~0.58 (tickets in last 90 days) |

So the data carries real but noisy signal. There is room above a naive model, and a hard ceiling below 1.0 because some churn is unpredictable by design.

## 7. Planted test cases (`ground_truth/planted_cases.json`, tenant `tnt_lumen`)

| Role | Renewal | Outcome | What is hidden in the mess |
|---|---|---|---|
| `lost_A` (lum-01289) | 2026-08-14 | churned | Slow decline from about 28 weeks out. **None of its 11 Zendesk tickets link to an organisation and Stripe has no Salesforce id**, so a naive join sees nothing, yet a cancellation-intent ticket sits there 8 weeks before the renewal. |
| `lost_B` (lum-02031) | 2026-09-09 | churned | Champion leaves 20 weeks out; emails to the champion bounce; usage collapses. The product workspace name is unrelated to the CRM name and the CRM website is blank. |
| `decoy_declining_renewed` (lum-00703) | 2026-08-27 | renewed | Same decline as lost_A, then saved. A hard negative. |
| `decoy_quiet_renewed` (lum-00437) | 2026-09-18 | renewed | Almost no data in any source, renews anyway. "Quiet" is not "unhealthy". |

The first two are the "two renewals we lost last quarter that nobody saw coming". Expectation: a good system ranks both in a CSM's top ~10 at least 8 weeks before renewal.

## 8. Ground truth (never read it in the pipeline)

`accounts_truth.csv` (archetype, ids in every source, missing sources, champion departure week) ·
`renewals_truth.csv` (outcome, pre-renewal health, true churn probability, whether churn was exogenous; pending renewals carry their hidden outcome) ·
`health_weekly.csv.gz` · `entity_links.csv` (every source record id → true account, `NOISE` for test and internal records; the answer key for entity resolution) · `planted_cases.json`.

## 9. Deliberately not modelled

Involuntary (dunning-driven) cancellations and win-backs (failed payments recover or stay open, but never cancel a subscription) · multi-currency and tax ·
Salesforce field history (only final state is exported) · schema drift between exports · GDPR deletions · email bodies and attachments · multi-product subscriptions and usage-based pricing ·
true user-level product telemetry volume (the stream is sampled) · seasonality beyond holidays and incidents · correlated shocks between accounts (an industry downturn).
Any of these would change real-world behaviour; none is needed to exercise the design decisions above.
