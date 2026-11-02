-- Agent state: runs, what changed since last week, and the approval queue of drafts. Idempotent (safe to apply every run).
create schema if not exists agent;

create table if not exists agent.run (
    run_id text primary key,                         -- 'agent-YYYYMMDD': one row per Monday, replaced on rerun
    asof timestamptz not null,
    score_run_id text,                               -- the score.run this agent run produced / consumed
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null check (status in ('ok', 'failed')),
    n_scored int, n_changes int, n_drafts int,
    error text);

create table if not exists agent.change (
    run_id text not null references agent.run (run_id) on delete cascade,
    tenant_id text not null, account_id text not null,
    kind text not null check (kind in ('entered_red', 'left_red', 'new_alert', 'risk_jump', 'csm_departed')),
    from_band text, to_band text, from_risk double precision, to_risk double precision,
    detail jsonb not null default '{}',
    primary key (run_id, tenant_id, account_id, kind));

create table if not exists agent.draft (
    draft_id bigserial primary key,
    run_id text not null references agent.run (run_id),
    tenant_id text not null, account_id text not null, owner_id text,
    kind text not null check (kind in ('csm_note', 'customer_email')),
    to_address text,
    subject text not null, body text not null,
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected', 'sent')),
    reasons jsonb not null,                          -- every fact and sentence the draft cites, from score.account_score only
    created_at timestamptz not null,                 -- logical time of the Monday run (not wall clock), so backfills dedupe correctly
    decided_by text, decided_at timestamptz, edited_body text,
    unique (run_id, tenant_id, account_id, kind));   -- a rerun of the same Monday cannot create a second copy

create index if not exists draft_queue on agent.draft (tenant_id, status, created_at desc);
create index if not exists draft_dedupe on agent.draft (tenant_id, account_id, kind, created_at);
create index if not exists change_tenant on agent.change (tenant_id, run_id);
