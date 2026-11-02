"""The only place the UI talks to Postgres.

    query(sql, params, tenant_id) -> list[dict]      (values are str/None; jsonb comes back as str, use j())

Every request sets the Postgres setting `app.tenant_id` first, so row-level security (agent/security.sql) can enforce the tenant;
the SQL in ui/ ALSO filters on tenant_id explicitly, so a missing policy never becomes a data leak (defence in depth).

Two drivers behind the same function:
  * psql subprocess (default; no dependencies, works in the build sandbox where PyPI is blocked)
  * psycopg  (DB_DRIVER=psycopg; deploy/requirements.txt installs it; written against the same contract, see DEPLOY.md)
Switching = set DB_DRIVER. SQL uses %(name)s parameters, which is psycopg's native style; the psql driver translates them into
psql variables (:'p_name'), which psql quotes itself, so values are never spliced into the SQL text by hand.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess

PARAM = re.compile(r"%\((\w+)\)s")


class DBError(RuntimeError):
    pass


def url() -> str:
    # the app should connect as the restricted role (fello_app); fall back to the owner only for local development
    return os.environ.get("APP_DATABASE_URL") or os.environ.get("DATABASE_URL") or "postgresql://fello:fello@localhost/fello_health"


def j(value, default=None):
    """jsonb column -> python. psql returns text, psycopg returns objects."""
    if value is None or value == "":
        return default
    return value if isinstance(value, (dict, list)) else json.loads(value)


def _to_psql(sql: str, params: dict):
    variables, names = [], {}

    def sub(m):
        k = m.group(1)
        v = params[k]
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        names[k] = str(v)
        return f":'p_{k}'"
    out = PARAM.sub(sub, sql)
    for k, v in names.items():
        variables += ["-v", f"p_{k}={v}"]
    return out, variables


def _run_psql(sql, params, tenant_id):
    body, variables = _to_psql(sql, params or {})
    script = ("\\set ON_ERROR_STOP on\n"
              "select set_config('app.tenant_id', :'ui_tenant', false) as _ \\gset\n" + body.rstrip().rstrip(";") + ";\n")
    p = subprocess.run(["psql", url(), "-X", "-q", "--csv", "-v", f"ui_tenant={tenant_id or ''}", *variables, "-f", "-"],
                       input=script, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise DBError(" | ".join(l for l in p.stderr.strip().splitlines() if l.strip()) or "psql failed")
    rows = list(csv.DictReader(io.StringIO(p.stdout)))
    return [{k: (v if v != "" else None) for k, v in r.items()} for r in rows]


def _run_psycopg(sql, params, tenant_id):               # pragma: no cover  (needs the psycopg package; see DEPLOY.md)
    import psycopg
    # The tenant is set LOCAL to one transaction (set_config(..., true)), so it can never leak to another request, even when a
    # pooler such as Neon's pgbouncer hands the same server connection to different clients.
    with psycopg.connect(url()) as conn:                # commits on exit, rolls back on error
        conn.execute("select set_config('app.tenant_id', %s, true)", (tenant_id or "",))
        cur = conn.execute(sql, params or {})
        if cur.description is None:
            return []
        cols = [c.name for c in cur.description]

        def cell(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return "t" if v else "f"                  # same text the psql driver returns
            return v if isinstance(v, (dict, list)) else str(v)
        return [{c: cell(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def query(sql: str, params: dict | None = None, tenant_id: str | None = None) -> list[dict]:
    run = _run_psycopg if os.environ.get("DB_DRIVER") == "psycopg" else _run_psql
    return run(sql, params or {}, tenant_id)


def table_exists(name: str) -> bool:
    return query("select (to_regclass(%(n)s) is not null) as t", {"n": name})[0]["t"] == "t"


def columns(name: str) -> list[str]:
    schema, table = name.split(".")
    return [r["column_name"] for r in query(
        "select column_name from information_schema.columns where table_schema = %(s)s and table_name = %(t)s order by ordinal_position",
        {"s": schema, "t": table})]
