"""Load data/raw/<tenant>/<source>/<entity>.jsonl into Postgres raw.<source>_<entity>.

    DATABASE_URL=postgresql://fello:fello@localhost/fello_health python -m ingest.load_raw --data data

Uses psql's \\copy (no Python DB driver needed; COPY is also the fastest bulk path).
Each file loads in its own transaction, so a failure leaves earlier files loaded and later runs resume safely.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
KNOWN = {  # (source, entity): table. An unknown file fails loudly rather than being silently ignored.
    ("salesforce", "accounts"), ("salesforce", "contacts"), ("salesforce", "opportunities"), ("salesforce", "users"),
    ("stripe", "customers"), ("stripe", "subscriptions"), ("stripe", "invoices"), ("stripe", "events"),
    ("zendesk", "organizations"), ("zendesk", "tickets"),
    ("product", "events"), ("product", "workspaces"), ("product", "users"),
    ("gmail", "messages"),
}


def psql(db_url: str, script: str) -> str:
    p = subprocess.run(["psql", db_url, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       input=script, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"psql failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def apply_schema(db_url: str) -> None:
    psql(db_url, (HERE / "schema.sql").read_text())


def load_file(db_url: str, tenant: str, source: str, entity: str, path: Path) -> tuple[int, int, int]:
    if (source, entity) not in KNOWN:
        raise ValueError(f"unknown source entity {source}.{entity} ({path}); add it to schema.sql and KNOWN")
    table = f"raw.{source}_{entity}"
    # Raw lines go through COPY CSV with quote/delimiter set to control characters that JSON can never contain,
    # so each line arrives as one untouched text value (COPY's default text format would mangle backslashes).
    script = f"""
begin;
create temp table _stage (line text) on commit drop;
\\copy _stage (line) from {lit(str(path))} with (format csv, quote e'\\x01', delimiter e'\\x02')
select count(*) as n_in from _stage \\gset
insert into raw.load_log (tenant_id, source, entity, source_file, rows_in_file)
    values ({lit(tenant)}, {lit(source)}, {lit(entity)}, {lit(str(path))}, :n_in) returning load_id \\gset
with ins as (
    insert into {table} (tenant_id, content_hash, synced_at, payload, load_id)
    select {lit(tenant)}, md5(line), (line::jsonb ->> '_synced_at')::timestamptz, line::jsonb, :load_id from _stage
    on conflict (tenant_id, content_hash) do nothing
    returning 1
) select count(*) as n_new from ins \\gset
update raw.load_log set finished_at = now(), rows_new = :n_new, rows_skipped = :n_in - :n_new where load_id = :load_id;
select :n_in, :n_new, :n_in - :n_new;
commit;
"""
    out = psql(db_url, script)
    a, b, c = (int(x) for x in out.split("|"))
    return a, b, c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args(argv)
    if not args.database_url:
        sys.exit("set DATABASE_URL or pass --database-url")
    raw = Path(args.data) / "raw"
    files = sorted(raw.glob("*/*/*.jsonl"))
    if not files:
        sys.exit(f"no files under {raw}")
    apply_schema(args.database_url)
    t0 = time.time()
    tot = [0, 0, 0]
    for f in files:
        tenant, source, entity = f.parent.parent.name, f.parent.name, f.stem
        n_in, n_new, n_skip = load_file(args.database_url, tenant, source, entity, f.resolve())
        tot = [tot[0] + n_in, tot[1] + n_new, tot[2] + n_skip]
        print(f"  {tenant:12s} {source}.{entity:14s} in_file={n_in:>8,}  new={n_new:>8,}  already_loaded={n_skip:>8,}", flush=True)
    print(f"done in {time.time() - t0:.1f}s | files={len(files)} rows_in_files={tot[0]:,} new={tot[1]:,} already_loaded={tot[2]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
