-- Lets the restricted web role read the login table. Safe to run repeatedly; does nothing until the role exists
-- (the role and the tenant row-level-security policies are created by agent/security.py).
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'fello_app') then
    grant usage on schema ui to fello_app;
    revoke all on all tables in schema ui from fello_app;
    grant select on ui.app_user to fello_app;
  end if;
end $$
