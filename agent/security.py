"""Create the application role and apply tenant isolation.   DATABASE_URL=... python -m agent.security

Roles are cluster-level and need a superuser; if the pipeline role cannot create them, this falls back to the local `postgres`
OS account (works in this sandbox and on a typical single-host install; on a managed database create the role once by hand).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ingest.load_raw import psql

HERE = Path(__file__).parent
ROLE_SQL = """do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'fello_app') then
    create role fello_app login password 'fello' nosuperuser nocreatedb nocreaterole nobypassrls;
  else
    alter role fello_app nosuperuser nobypassrls;
  end if; end $$"""


def ensure_role(db_url: str) -> None:
    try:
        psql(db_url, ROLE_SQL)
        return
    except RuntimeError as first:
        p = subprocess.run(["su", "postgres", "-c", "psql -X -q -v ON_ERROR_STOP=1 -f -"], input=ROLE_SQL, text=True, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError(f"cannot create role fello_app: {first}\nfallback: {p.stderr.strip()}")


def apply(db_url: str) -> str:
    from .db import ensure_schema
    from scoring.score import DDL as SCORE_DDL
    ensure_schema(db_url)
    psql(db_url, SCORE_DDL)                      # the score tables must exist before policies can be attached
    ensure_role(db_url)
    owner = psql(db_url, "select current_user")
    psql(db_url, (HERE / "security.sql").read_text().replace("{owner}", owner))
    return owner


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    print(f"tenant isolation applied (pipeline role {apply(a.database_url)}, app role fello_app)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
