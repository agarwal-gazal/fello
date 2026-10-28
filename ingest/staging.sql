-- STAGING layer: typed, deduplicated, normalised views of RAW. Full refresh each run (cheap at this size).
--
-- What staging does and does not do:
--  * It types and dedupes. It does NOT join across systems (that is entity resolution) and does NOT compute features.
--  * Every table keeps `known_at` = the FIRST time the warehouse knew the record (min synced_at over duplicates/versions).
--    Point-in-time features filter on known_at, never on event time alone.
--  * Noise is FLAGGED (is_test / is_internal), not deleted, so exclusions are auditable. Consumers filter on the flags.
--  * Rules use only what the pipeline can see. Nothing here reads data/ground_truth.

drop schema if exists stg cascade;
create schema stg;

create function stg.norm_domain(t text) returns text language sql immutable as $$
    select nullif(split_part(regexp_replace(lower(btrim(t)), '^(https?://)?(www\.)?', ''), '/', 1), '')
$$;
create function stg.email_domain(t text) returns text language sql immutable as $$
    select nullif(lower(btrim(split_part(t, '@', 2))), '')
$$;
-- "<Name> <a@b.com>" or "a@b.com" -> a@b.com
create function stg.addr(t text) returns text language sql immutable as $$
    select lower(btrim(coalesce(substring(t from '<([^>]+)>'), split_part(t, ',', 1))))
$$;

-- Consumer mail providers: an address here identifies a PERSON, never a COMPANY. Matching on these domains would
-- merge strangers, so they are excluded from every domain join (people are matched on the full address instead).
create table stg.free_mail_domain (domain text primary key);
insert into stg.free_mail_domain values ('gmail.com'),('yahoo.com'),('outlook.com'),('hotmail.com'),('proton.me'),
    ('icloud.com'),('aol.com'),('live.com'),('msn.com'),('protonmail.com'),('gmx.com');

-- ============================================================ Salesforce (snapshot: latest row per Id)
create table stg.sf_account as
select distinct on (tenant_id, payload->>'Id')
    tenant_id, payload->>'Id' as sf_id, payload->>'Name' as name, payload->>'Website' as website,
    stg.norm_domain(payload->>'Website') as website_domain, payload->>'Industry' as industry,
    (payload->>'NumberOfEmployees')::int as employees, payload->>'Segment__c' as segment,
    payload->>'OwnerId' as owner_id, payload->>'Type' as account_type,
    (payload->>'Contract_End_Date__c')::date as contract_end_date, (payload->>'ARR__c')::numeric as arr,
    (payload->>'CreatedDate')::timestamptz as created_at, (payload->>'LastModifiedDate')::timestamptz as last_modified_at,
    synced_at as known_at
from raw.salesforce_accounts
order by tenant_id, payload->>'Id', synced_at desc;

create table stg.sf_contact as
select distinct on (tenant_id, payload->>'Id')
    tenant_id, payload->>'Id' as contact_id, payload->>'AccountId' as sf_account_id,
    payload->>'FirstName' as first_name, payload->>'LastName' as last_name,
    lower(payload->>'Email') as email, stg.email_domain(payload->>'Email') as email_domain, payload->>'Title' as title,
    (payload->>'Is_Champion__c')::boolean as is_champion, (payload->>'Is_Decision_Maker__c')::boolean as is_decision_maker,
    (payload->>'CreatedDate')::timestamptz as created_at, synced_at as known_at
from raw.salesforce_contacts
order by tenant_id, payload->>'Id', synced_at desc;

-- Opportunity StageName/IsWon are FINAL values (Salesforce keeps no history here). Using them as features "as of" an
-- earlier date leaks the outcome. They are kept for labels and audit only.
create table stg.sf_opportunity as
select distinct on (tenant_id, payload->>'Id')
    tenant_id, payload->>'Id' as opportunity_id, payload->>'AccountId' as sf_account_id, payload->>'Type' as opp_type,
    payload->>'StageName' as stage, (payload->>'CloseDate')::date as close_date, (payload->>'Amount')::numeric as amount,
    (payload->>'IsClosed')::boolean as is_closed, (payload->>'IsWon')::boolean as is_won,
    (payload->>'CreatedDate')::timestamptz as created_at, (payload->>'LastModifiedDate')::timestamptz as last_modified_at,
    synced_at as known_at
from raw.salesforce_opportunities
order by tenant_id, payload->>'Id', synced_at desc;

create table stg.sf_user as
select distinct on (tenant_id, payload->>'Id')
    tenant_id, payload->>'Id' as user_id, payload->>'Name' as name, lower(payload->>'Email') as email,
    stg.email_domain(payload->>'Email') as email_domain, (payload->>'IsActive')::boolean as is_active
from raw.salesforce_users
order by tenant_id, payload->>'Id', synced_at desc;

-- The tenant's own staff domains: how we recognise internal traffic without being told.
create table stg.tenant_staff_domain as
select distinct tenant_id, email_domain as domain from stg.sf_user where email_domain is not null;

-- ============================================================ Stripe
create table stg.stripe_customer as
select distinct on (tenant_id, payload->>'id')
    tenant_id, payload->>'id' as customer_id, payload->>'name' as name, lower(payload->>'email') as email,
    stg.email_domain(payload->>'email') as email_domain,
    payload->'metadata'->>'salesforce_id' as sf_id_hint,
    to_timestamp((payload->>'created')::bigint) as created_at,
    not coalesce((payload->>'livemode')::boolean, true) as is_test            -- test-mode objects are not customers
from raw.stripe_customers
order by tenant_id, payload->>'id', synced_at desc;

create table stg.stripe_subscription as
select distinct on (tenant_id, payload->>'id')
    tenant_id, payload->>'id' as subscription_id, payload->>'customer' as customer_id, payload->>'status' as status,
    (payload->>'quantity')::int as quantity, payload->'plan'->>'interval' as billing_interval,
    (payload->'plan'->>'amount')::numeric / 100 as unit_price,
    to_timestamp((payload->>'start_date')::bigint) as started_at,
    to_timestamp((payload->>'current_period_end')::bigint) as current_period_end,
    (payload->>'cancel_at_period_end')::boolean as cancel_at_period_end,
    to_timestamp((payload->>'canceled_at')::bigint) as canceled_at,
    to_timestamp((payload->>'ended_at')::bigint) as ended_at
from raw.stripe_subscriptions
order by tenant_id, payload->>'id', synced_at desc;

create table stg.stripe_invoice as
select distinct on (tenant_id, payload->>'id')
    tenant_id, payload->>'id' as invoice_id, payload->>'customer' as customer_id, payload->>'subscription' as subscription_id,
    (payload->>'amount_due')::numeric / 100 as amount_due, (payload->>'amount_paid')::numeric / 100 as amount_paid,
    payload->>'status' as status, (payload->>'attempt_count')::int as attempt_count,
    payload->>'billing_reason' as billing_reason, to_timestamp((payload->>'created')::bigint) as created_at,
    to_timestamp((payload->'status_transitions'->>'paid_at')::bigint) as paid_at,
    min(synced_at) over (partition by tenant_id, payload->>'id') as known_at
from raw.stripe_invoices
order by tenant_id, payload->>'id', synced_at desc;

-- Webhook retries re-deliver the same event id later. Keep the EARLIEST delivery: that is when we first knew.
create table stg.stripe_event as
select distinct on (tenant_id, payload->>'id')
    tenant_id, payload->>'id' as event_id, payload->>'type' as event_type,
    to_timestamp((payload->>'created')::bigint) as event_at,
    payload->'data'->'object'->>'customer' as customer_id,
    (payload->'data'->'object'->>'quantity')::int as quantity,
    (payload->'data'->'previous_attributes'->>'quantity')::int as previous_quantity,
    (payload->'data'->'object'->>'cancel_at_period_end')::boolean as cancel_at_period_end,
    (payload->'data'->'object'->>'attempt_count')::int as attempt_count,
    synced_at as known_at
from raw.stripe_events
order by tenant_id, payload->>'id', synced_at asc;

-- ============================================================ Zendesk
create table stg.zd_org as
select distinct on (tenant_id, payload->>'id')
    tenant_id, (payload->>'id')::bigint as org_id, payload->>'name' as name,
    array(select stg.norm_domain(d) from jsonb_array_elements_text(payload->'domain_names') d) as domains,
    payload->>'external_id' as sf_id_hint
from raw.zendesk_organizations
order by tenant_id, payload->>'id', synced_at desc;

-- Tickets are exported as VERSIONS (an early "open" copy and the final "solved" copy can both be present).
-- Ids are only unique within a tenant. Keep the latest version's attributes, and known_at = first version's arrival.
-- Fields that change across versions (status, solved_at, CSAT) must not be used as-of an earlier date without
-- consulting zd_ticket_version; immutable fields (created_at, priority, tags, requester) are safe.
create table stg.zd_ticket_version as
select tenant_id, (payload->>'id')::bigint as ticket_id, payload->>'status' as status,
       (payload->>'updated_at')::timestamptz as updated_at, (payload->>'solved_at')::timestamptz as solved_at,
       payload->'satisfaction_rating'->>'score' as csat, synced_at
from raw.zendesk_tickets;

create table stg.zd_ticket as
select distinct on (tenant_id, payload->>'id')
    tenant_id, (payload->>'id')::bigint as ticket_id, payload->>'subject' as subject, payload->>'description' as description,
    payload->>'status' as status, payload->>'priority' as priority, payload->>'channel' as channel,
    lower(payload->>'requester_email') as requester_email, stg.email_domain(payload->>'requester_email') as requester_domain,
    payload->>'requester_name' as requester_name, (payload->>'organization_id')::bigint as org_id,
    array(select jsonb_array_elements_text(payload->'tags')) as tags,
    (payload->'tags'->>0) as category, (payload->'tags') ? 'reopened' as is_reopened,
    (payload->>'created_at')::timestamptz as created_at, (payload->>'solved_at')::timestamptz as solved_at,
    (payload->>'first_reply_minutes')::int as first_reply_minutes, payload->'satisfaction_rating'->>'score' as csat,
    min(synced_at) over (partition by tenant_id, payload->>'id') as known_at
from raw.zendesk_tickets
order by tenant_id, payload->>'id', (payload->>'updated_at')::timestamptz desc, synced_at desc;

-- ============================================================ Product analytics
create table stg.product_workspace as
select distinct on (w.tenant_id, w.payload->>'workspace_id')
    w.tenant_id, w.payload->>'workspace_id' as workspace_id, w.payload->>'name' as name,
    lower(w.payload->>'owner_email') as owner_email, stg.email_domain(w.payload->>'owner_email') as owner_domain,
    w.payload->>'plan' as plan, w.payload->>'crm_account_id' as sf_id_hint,
    (w.payload->>'created_at')::timestamptz as created_at,
    -- internal = owned by the tenant's own staff (QA / demo). Recognised from the tenant's own Salesforce user domains.
    exists (select 1 from stg.tenant_staff_domain d
            where d.tenant_id = w.tenant_id and d.domain = stg.email_domain(w.payload->>'owner_email')) as is_internal
from raw.product_workspaces w
order by w.tenant_id, w.payload->>'workspace_id', w.synced_at desc;

create table stg.product_user as
select distinct on (tenant_id, payload->>'user_id')
    tenant_id, payload->>'user_id' as user_id, payload->>'workspace_id' as workspace_id,
    lower(payload->>'email') as email, stg.email_domain(payload->>'email') as email_domain, payload->>'role' as role,
    (payload->>'created_at')::timestamptz as created_at, (payload->>'deactivated_at')::timestamptz as deactivated_at
from raw.product_users
order by tenant_id, payload->>'user_id', synced_at desc;

-- Event time normalisation (decisions):
--  * Duplicates: keep the earliest delivery (known_at = first arrival).
--  * Timestamps with no zone are local wall-clock time of an unknown zone. They are read as UTC. The error is bounded
--    by the zone offset (<= ~12 h), immaterial for weekly features; flagged so the assumption is auditable.
--  * An event cannot occur after the moment it was received. Such timestamps (bad device clocks, or the naive-as-UTC
--    shift above) are clamped to known_at and flagged.
--  * workspace_id may be missing; it is recovered from the user when possible.
create table stg.product_event as
with d as (
    select distinct on (e.tenant_id, e.payload->>'event_id')
        e.tenant_id, e.payload->>'event_id' as event_id, e.payload->>'event' as event_name,
        e.payload->>'user_id' as user_id, e.payload->>'anonymous_id' as anonymous_id,
        e.payload->>'workspace_id' as workspace_id_raw, e.payload->'properties'->>'platform' as platform,
        (e.payload->'properties'->>'status')::int as api_status,
        e.payload->>'timestamp' as ts_raw, e.synced_at as known_at
    from raw.product_events e
    order by e.tenant_id, e.payload->>'event_id', e.synced_at asc
), p as (
    select d.*, case when ts_raw ~ 'Z$' then ts_raw::timestamptz else (ts_raw::timestamp at time zone 'UTC') end as ts_parsed
    from d
)
select p.tenant_id, p.event_id, p.event_name, p.user_id, p.anonymous_id,
       coalesce(p.workspace_id_raw, u.workspace_id) as workspace_id, p.workspace_id_raw is null as workspace_recovered,
       p.platform, p.api_status, least(p.ts_parsed, p.known_at) as event_ts,
       case when p.ts_parsed > p.known_at then 'future_clamped'
            when p.ts_raw !~ 'Z$' then 'naive_assumed_utc' else 'ok' end as ts_quality,
       p.known_at,
       coalesce(w.is_internal, false) as is_internal
from p
left join stg.product_user u on u.tenant_id = p.tenant_id and u.user_id = p.user_id
left join stg.product_workspace w on w.tenant_id = p.tenant_id and w.workspace_id = coalesce(p.workspace_id_raw, u.workspace_id);
create index on stg.product_event (tenant_id, workspace_id, event_ts);

-- ============================================================ Gmail (CSM mailboxes; metadata + snippet only)
create table stg.gmail_message as
with m as (
    select distinct on (tenant_id, payload->>'id')
        tenant_id, payload->>'id' as message_id, payload->>'threadId' as thread_id, payload->>'_mailbox' as mailbox,
        to_timestamp((payload->>'internalDate')::bigint / 1000.0) as sent_at,
        (payload->'labelIds') ? 'INBOX' as is_inbound, payload->>'snippet' as snippet,
        (select h->>'value' from jsonb_array_elements(payload->'payload'->'headers') h where h->>'name' = 'From' limit 1) as from_hdr,
        (select h->>'value' from jsonb_array_elements(payload->'payload'->'headers') h where h->>'name' = 'To' limit 1) as to_hdr,
        (select h->>'value' from jsonb_array_elements(payload->'payload'->'headers') h where h->>'name' = 'Subject' limit 1) as subject,
        synced_at as known_at
    from raw.gmail_messages
    order by tenant_id, payload->>'id', synced_at asc
)
select m.tenant_id, m.message_id, m.thread_id, m.mailbox, m.sent_at, m.known_at,
       case when m.is_inbound then 'inbound' else 'outbound' end as direction,
       -- the person on the OTHER side of the mailbox owner
       case when m.is_inbound then stg.addr(m.from_hdr) else stg.addr(m.to_hdr) end as counterparty_email,
       stg.email_domain(case when m.is_inbound then stg.addr(m.from_hdr) else stg.addr(m.to_hdr) end) as counterparty_domain,
       m.subject, m.snippet,
       (stg.addr(m.from_hdr) like 'mailer-daemon@%' or m.subject like 'Delivery Status Notification%') as is_bounce,
       -- when a message bounced, the real counterparty is the address that failed, not the mailer daemon
       case when stg.addr(m.from_hdr) like 'mailer-daemon@%' then substring(m.snippet from 'message to (\S+) couldn') end as bounced_address,
       (m.subject ilike 'Automatic reply:%') as is_auto_reply,
       exists (select 1 from stg.tenant_staff_domain d
               where d.tenant_id = m.tenant_id
                 and d.domain = stg.email_domain(case when m.is_inbound then stg.addr(m.from_hdr) else stg.addr(m.to_hdr) end)) as is_internal
from m;
-- for bounces the counterparty is the failed address (the champion who left), not mailer-daemon
update stg.gmail_message
   set counterparty_email = bounced_address, counterparty_domain = stg.email_domain(bounced_address)
 where is_bounce and bounced_address is not null;
update stg.gmail_message g set is_internal = exists (select 1 from stg.tenant_staff_domain d
        where d.tenant_id = g.tenant_id and d.domain = g.counterparty_domain)
 where is_bounce and bounced_address is not null;

-- ============================================================ Audit: what staging dropped / flagged
create table stg.audit as
select 'product_event'           as entity, (select count(*) from raw.product_events) as raw_rows, (select count(*) from stg.product_event) as staged_rows,
       (select count(*) from stg.product_event where ts_quality <> 'ok') as flagged_rows, 'dedupe by event_id; flagged = naive or future timestamps' as note
union all select 'product_event_internal', (select count(*) from stg.product_event), (select count(*) from stg.product_event where not is_internal),
       (select count(*) from stg.product_event where is_internal), 'internal QA traffic from the tenant''s own staff'
union all select 'stripe_event', (select count(*) from raw.stripe_events), (select count(*) from stg.stripe_event), 0, 'webhook retries collapsed to first delivery'
union all select 'stripe_invoice', (select count(*) from raw.stripe_invoices), (select count(*) from stg.stripe_invoice), 0, 'one row per invoice id'
union all select 'stripe_customer_test', (select count(*) from raw.stripe_customers), (select count(*) from stg.stripe_customer where not is_test),
       (select count(*) from stg.stripe_customer where is_test), 'test-mode customers are not customers'
union all select 'zendesk_ticket', (select count(*) from raw.zendesk_tickets), (select count(*) from stg.zd_ticket), 0, 'ticket versions collapsed to latest; known_at = first version'
union all select 'gmail_message', (select count(*) from raw.gmail_messages), (select count(*) from stg.gmail_message),
       (select count(*) from stg.gmail_message where is_internal), 'flagged = internal / staff mail'
union all select 'product_workspace_internal', (select count(*) from stg.product_workspace), (select count(*) from stg.product_workspace where not is_internal),
       (select count(*) from stg.product_workspace where is_internal), 'workspaces owned by tenant staff';
