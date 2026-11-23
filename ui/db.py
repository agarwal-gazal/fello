"""The only place the UI talks to Postgres.

    query(sql, params, tenant_id) -> list[dict]      (values are str/None; jsonb comes back as str, use j())

Every request sets the Postgres setting `app.tenant_id` first, so row-level security (agent/security.sql) can enforce the tenant;
the SQL in ui/ ALSO filters on tenant_id explicitly, so a missing policy never becomes a data leak (defence in depth).

Two drivers behind the same function:
  * psql subprocess (default; no dependencies, works in the build sandbox where PyPI is blocked)
  * psycopg  (DB_DRIVER=psycopg; deploy/requirements.txt installs it; same contract, see DEPLOY.md). Idle connections are kept for
    reuse (DB_POOL_SIZE, default 8); a connection that saw an error is closed, never reused.
Switching = set DB_DRIVER. SQL uses %(name)s parameters, which is psycopg's native style; the psql driver translates them into
psql variables (:'p_name'), which psql quotes itself, so values are never spliced into the SQL text by hand.
"""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import subprocess
import threading

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


POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
_POOLS: dict = {}                                       # url -> idle connections; a page makes several queries, each used to open a connection
_POOLS_LOCK = threading.Lock()


def _cell(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "t" if v else "f"                      # same text the psql driver returns
    return v if isinstance(v, (dict, list)) else str(v)


def _run_psycopg(sql, params, tenant_id):
    import psycopg
    u = url()
    with _POOLS_LOCK:
        pool = _POOLS.setdefault(u, queue.LifoQueue(maxsize=POOL_SIZE))
    conn = None
    while conn is None:
        try:
            conn = pool.get_nowait()
            if conn.closed or conn.broken:            # dropped by the server while idle
                conn = None
        except queue.Empty:
            try:
                conn = psycopg.connect(u)
            except psycopg.Error as ex:
                raise DBError(str(ex)) from ex
    reusable = False
    try:
        # The tenant is set LOCAL to one transaction (set_config(..., true)), so it can never leak to the next query on this pooled
        # connection, nor to another client when a pooler such as Neon's pgbouncer shares the server connection.
        with conn.transaction():                       # commits on exit, rolls back on error
            conn.execute("select set_config('app.tenant_id', %s, true)", (tenant_id or "",))
            cur = conn.execute(sql, params or {})
            rows = [] if cur.description is None else [{c.name: _cell(v) for c, v in zip(cur.description, row)} for row in cur.fetchall()]
        reusable = True
        return rows
    except psycopg.Error as ex:                        # callers handle DBError (e.g. agent tables missing), whichever driver is on
        raise DBError(str(ex).strip()) from ex
    finally:
        if reusable and not conn.closed:
            try:
                pool.put_nowait(conn)
            except queue.Full:
                conn.close()
        else:
            conn.close()                               # never hand a connection in an unknown state to the next request


def query(sql: str, params: dict | None = None, tenant_id: str | None = None) -> list[dict]:
    run = _run_psycopg if os.environ.get("DB_DRIVER") == "psycopg" else _run_psql
    return run(sql, params or {}, tenant_id)
