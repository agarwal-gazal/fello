"""Build the core layer from staging + entity resolution.   DATABASE_URL=... python -m core.build_core [--export-at ...]"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ingest.load_raw import psql

HERE = Path(__file__).parent


def build(db_url: str, export_at: str) -> str:
    sql = (HERE / "core.sql").read_text().replace(":'export_at'", f"'{export_at}'")
    psql(db_url, sql)
    return psql(db_url, """
        select 'account', count(*) from core.account union all select 'contact', count(*) from core.contact
        union all select 'ticket', count(*) from core.ticket union all select 'email', count(*) from core.email
        union all select 'product_event', count(*) from core.product_event union all select 'invoice', count(*) from core.invoice
        union all select 'billing_event', count(*) from core.billing_event union all select 'renewal', count(*) from core.renewal""")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--data", default="data")
    ap.add_argument("--export-at", default=None)
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    export_at = a.export_at or json.loads((Path(a.data) / "manifest.json").read_text())["export_at"]
    t0 = time.time()
    for line in build(a.database_url, export_at).splitlines():
        k, n = line.split("|")
        print(f"{k:16s}{int(n):>10,}")
    print(f"core built in {time.time() - t0:.1f}s (export_at={export_at})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
