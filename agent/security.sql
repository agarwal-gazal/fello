-- Tenant isolation. Applied idempotently by agent/security.py.  {owner} = the pipeline role that owns the tables.
--
-- Two roles, two kinds of access:
--   {owner}     the pipeline (loader, scorer, agent). Trusted, sees every tenant, writes everything.
--   fello_app   the web application. NOT superuser, NOT bypassrls. Sees only rows whose tenant_id equals the
--               session setting app.tenant_id, which the app sets with set_config() right after connecting.
-- With app.tenant_id unset, current_setting(..., true) is NULL and the comparison is NULL: zero rows, not an error and not "all".

grant usage on schema score, agent to fello_app;
revoke all on all tables in schema score, agent from fello_app;
revoke all on all sequences in schema agent from fello_app;

-- tenant-keyed tables ------------------------------------------------------------------------------------------------
alter table score.account_score enable row level security;
alter table agent.change        enable row level security;
alter table agent.draft         enable row level security;
-- FORCE (so even the owner is bound by policies) on the agent tables, which the agent writes with INSERT.
-- Not on score.account_score: its loader uses COPY, which Postgres refuses on a table with forced RLS.
alter table agent.change force row level security;
alter table agent.draft  force row level security;

drop policy if exists pipeline_all on score.account_score;
drop policy if exists tenant_isolation on score.account_score;
create policy pipeline_all on score.account_score to {owner} using (true) with check (true);
create policy tenant_isolation on score.account_score to fello_app
    using (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists pipeline_all on agent.change;
drop policy if exists tenant_isolation on agent.change;
create policy pipeline_all on agent.change to {owner} using (true) with check (true);
create policy tenant_isolation on agent.change to fello_app
    using (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists pipeline_all on agent.draft;
drop policy if exists tenant_isolation on agent.draft;
create policy pipeline_all on agent.draft to {owner} using (true) with check (true);
create policy tenant_isolation on agent.draft to fello_app
    using (tenant_id = current_setting('app.tenant_id', true))
    with check (tenant_id = current_setting('app.tenant_id', true));

-- run tables carry no tenant column (a run covers every tenant). A run is visible exactly when the caller can see
-- at least one of its scored rows, so the tenant filter above does the work and nothing can leak through a run row.
alter table score.run enable row level security;
alter table agent.run enable row level security;
alter table agent.run force row level security;

drop policy if exists pipeline_all on score.run;
drop policy if exists tenant_isolation on score.run;
create policy pipeline_all on score.run to {owner} using (true) with check (true);
create policy tenant_isolation on score.run to fello_app
    using (exists (select 1 from score.account_score s where s.run_id = score.run.run_id));

drop policy if exists pipeline_all on agent.run;
drop policy if exists tenant_isolation on agent.run;
create policy pipeline_all on agent.run to {owner} using (true) with check (true);
create policy tenant_isolation on agent.run to fello_app
    using (exists (select 1 from score.account_score s where s.run_id = agent.run.score_run_id));

-- privileges (RLS filters rows; grants decide what may be done to them) ------------------------------------------------
grant select on score.account_score, score.run to fello_app;
grant select on agent.change, agent.run to fello_app;
grant select on agent.draft to fello_app;
grant update (status, decided_by, decided_at, edited_body) on agent.draft to fello_app;
