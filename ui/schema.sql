-- Login accounts. Outside row-level security on purpose: a login has to be looked up BEFORE we know which tenant it belongs to.
create schema if not exists ui;
create table if not exists ui.app_user (
    username     text primary key,
    tenant_id    text not null,
    role         text not null check (role in ('csm', 'manager')),
    owner_id     text,                         -- Salesforce user id of the CSM (csm role); managers see the whole tenant
    display_name text not null,
    pw_hash      text not null                 -- scrypt$n$r$p$salt$hash, never the password
);
