-- Core layer: staging + entity resolution => one row per business fact, keyed by (tenant_id, account_id).
-- Rebuilt from scratch on every run. Every fact table keeps BOTH event time (when it happened) and known_at
-- (when the warehouse learned of it). Features may only use facts with known_at <= the snapshot time.
-- :export_at is the moment the data was exported ("now" for this dataset).
drop schema if exists core cascade;
create schema core;

create table core.meta as select :'export_at'::timestamptz as export_at;

-- ---------------------------------------------------------------------------------------------- accounts
-- Attributes come from the canonical (earliest) Salesforce record. Duplicates are folded in by er.sf_merge.
create table core.account as
select a.tenant_id, a.sf_id as account_id, a.name, a.industry, a.employees, a.segment, a.owner_id,
       u.name as csm_name, coalesce(u.is_active, false) as csm_active,
       a.account_type, a.arr as arr_crm, a.contract_end_date as contract_end_crm, a.created_at,
       (select count(*) from er.sf_merge m where m.tenant_id = a.tenant_id and m.account_id = a.sf_id) as n_crm_records
from stg.sf_account a
join er.account e on e.tenant_id = a.tenant_id and e.account_id = a.sf_id
left join stg.sf_user u on u.tenant_id = a.tenant_id and u.user_id = a.owner_id;
alter table core.account add primary key (tenant_id, account_id);

create table core.contact as
select c.tenant_id, m.account_id, c.contact_id, c.first_name, c.last_name, c.email, c.title,
       coalesce(c.is_champion, false) as is_champion, coalesce(c.is_decision_maker, false) as is_decision_maker,
       c.created_at, c.known_at
from stg.sf_contact c join er.sf_merge m on m.tenant_id = c.tenant_id and m.sf_id = c.sf_account_id;
create index on core.contact (tenant_id, account_id);

-- ---------------------------------------------------------------------------------------------- facts
-- Each fact table inherits the link's method/confidence/conflict so a number on screen can be traced to how the record was attributed.
create table core.ticket as
select t.tenant_id, l.account_id, t.ticket_id, t.created_at, t.known_at, t.solved_at, t.subject, t.status, t.priority,
       t.category, t.tags, t.is_reopened, t.first_reply_minutes, t.csat, t.requester_email,
       l.method as link_method, l.confidence as link_confidence, l.conflict as link_conflict
from stg.zd_ticket t
join er.link l on l.tenant_id = t.tenant_id and l.source_entity = 'zendesk_ticket' and l.source_record_id = t.ticket_id::text
where l.account_id is not null;
create index on core.ticket (tenant_id, account_id, created_at);

create table core.email as
select g.tenant_id, l.account_id, g.message_id, g.thread_id, g.mailbox, g.sent_at, g.known_at, g.direction,
       g.counterparty_email, g.subject, g.snippet, g.is_bounce, g.bounced_address, g.is_auto_reply,
       l.method as link_method, l.confidence as link_confidence, l.conflict as link_conflict
from stg.gmail_message g
join er.link l on l.tenant_id = g.tenant_id and l.source_entity = 'gmail_message' and l.source_record_id = g.message_id
where l.account_id is not null and not g.is_internal;
create index on core.email (tenant_id, account_id, sent_at);

create table core.product_event as
select p.tenant_id, l.account_id, p.event_id, p.event_name, p.user_id, p.event_ts, p.known_at, p.ts_quality, p.platform, p.api_status,
       l.conflict as link_conflict
from stg.product_event p
join er.link l on l.tenant_id = p.tenant_id and l.source_entity = 'product_workspace' and l.source_record_id = p.workspace_id
where l.account_id is not null and not p.is_internal;
create index on core.product_event (tenant_id, account_id, event_ts);

create table core.invoice as
select i.tenant_id, l.account_id, i.invoice_id, i.subscription_id, i.amount_due, i.amount_paid, i.status, i.attempt_count,
       i.billing_reason, i.created_at, i.paid_at, i.known_at
from stg.stripe_invoice i
join er.link l on l.tenant_id = i.tenant_id and l.source_entity = 'stripe_customer' and l.source_record_id = i.customer_id
where l.account_id is not null;
create index on core.invoice (tenant_id, account_id, created_at);

create table core.billing_event as
select e.tenant_id, l.account_id, e.event_id, e.event_type, e.event_at, e.known_at, e.quantity, e.previous_quantity,
       e.cancel_at_period_end, e.attempt_count
from stg.stripe_event e
join er.link l on l.tenant_id = e.tenant_id and l.source_entity = 'stripe_customer' and l.source_record_id = e.customer_id
where l.account_id is not null;
create index on core.billing_event (tenant_id, account_id, event_at);

-- ---------------------------------------------------------------------------------------------- renewals and outcomes
-- The renewal DATE comes from the CRM renewal opportunity (the anniversary), the OUTCOME from Stripe, because the CRM stage is
-- unreliable: opportunities are often never closed (about 8% of cancellations sit on a still-open opportunity).
--   churned    : Stripe deleted the subscription on the renewal date
--   downgraded : Stripe cut the seat count by >= 25% on the renewal date
--   renewed    : neither, once the renewal date is old enough that late-arriving events have landed (14 days)
--   pending    : renewal date in the future, or too recent to call
create table core.renewal as
with opp as (
    select o.tenant_id, m.account_id, o.close_date as renewal_date, o.stage as crm_stage,
           row_number() over (partition by o.tenant_id, m.account_id, o.close_date order by o.is_closed desc, o.last_modified_at desc) rn
    from stg.sf_opportunity o
    join er.sf_merge m on m.tenant_id = o.tenant_id and m.sf_id = o.sf_account_id
    where o.opp_type = 'Renewal'
), r as (select * from opp where rn = 1)
select r.tenant_id, r.account_id, r.renewal_date, r.crm_stage,
       row_number() over (partition by r.tenant_id, r.account_id order by r.renewal_date) as renewal_number,
       case
         when exists (select 1 from core.billing_event b where b.tenant_id = r.tenant_id and b.account_id = r.account_id
                      and b.event_type = 'customer.subscription.deleted' and abs(b.event_at::date - r.renewal_date) <= 3) then 'churned'
         when exists (select 1 from core.billing_event b where b.tenant_id = r.tenant_id and b.account_id = r.account_id
                      and b.event_type = 'customer.subscription.updated' and b.previous_quantity > 0
                      and b.quantity <= 0.75 * b.previous_quantity and abs(b.event_at::date - r.renewal_date) <= 3) then 'downgraded'
         when r.renewal_date > (select export_at::date - 14 from core.meta) then 'pending'
         -- Stripe is not linked for this account at all: fall back to the (less reliable) CRM stage
         when not exists (select 1 from core.billing_event b where b.tenant_id = r.tenant_id and b.account_id = r.account_id)
              and r.crm_stage = 'Closed Lost' then 'churned'
         else 'renewed'
       end as outcome
from r;
alter table core.renewal add primary key (tenant_id, account_id, renewal_date);
alter table core.renewal add column is_label boolean generated always as (outcome in ('churned','downgraded','renewed')) stored;

-- first date the warehouse knew about the account at all (cold-start guard)
create table core.account_first_seen as
select tenant_id, account_id, min(t) as first_seen from (
    select tenant_id, account_id, created_at as t from core.account
    union all select tenant_id, account_id, min(event_ts) from core.product_event group by 1, 2
    union all select tenant_id, account_id, min(created_at) from core.invoice group by 1, 2) x
group by 1, 2;

analyze;
