-- RAW layer: an append-only, verbatim landing zone. One table per source entity, all identical in shape.
--
-- Design rules (each one is a decision to defend):
--  * payload is the record exactly as exported (jsonb). Nothing is cleaned, deduped or re-typed here.
--  * synced_at is the ONE typed column: when the record reached the warehouse (knowledge time).
--    Every point-in-time feature downstream filters on it, so it is NOT NULL and a load fails without it.
--  * identity = (tenant_id, md5 of the raw line). Re-loading the same export is a no-op (idempotent),
--    while real source-side duplicates (webhook retries, Zendesk ticket versions, re-sent product events)
--    carry a different _synced_at, so they are KEPT as separate rows for the staging layer to resolve.
--  * tenant_id is the first column of every key, so tenant isolation (RLS) and partitioning can hang off it later.

create schema if not exists raw;

create table if not exists raw.load_log (
    load_id      bigint generated always as identity primary key,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    tenant_id    text not null,
    source       text not null,
    entity       text not null,
    source_file  text not null,
    rows_in_file bigint,
    rows_new     bigint,
    rows_skipped bigint
);

do $$
declare
    t text;
begin
    foreach t in array array[
        'salesforce_accounts','salesforce_contacts','salesforce_opportunities','salesforce_users',
        'stripe_customers','stripe_subscriptions','stripe_invoices','stripe_events',
        'zendesk_organizations','zendesk_tickets',
        'product_events','product_workspaces','product_users',
        'gmail_messages']
    loop
        execute format($f$
            create table if not exists raw.%I (
                tenant_id    text        not null,
                content_hash text        not null,
                synced_at    timestamptz not null,
                payload      jsonb       not null,
                load_id      bigint      not null references raw.load_log(load_id),
                primary key (tenant_id, content_hash)
            )$f$, t);
        execute format('create index if not exists %I on raw.%I (tenant_id, synced_at)', t || '_synced_idx', t);
    end loop;
end $$;
