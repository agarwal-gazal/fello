"""Rebuild the staging schema from raw.   DATABASE_URL=... python -m ingest.build_staging"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .load_raw import psql

HERE = Path(__file__).parent


def build(db_url: str) -> str:
    psql(db_url, (HERE / "staging.sql").read_text())
    return psql(db_url, "select entity, raw_rows, staged_rows, flagged_rows from stg.audit order by entity")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args(argv)
    if not args.database_url:
        sys.exit("set DATABASE_URL or pass --database-url")
    t0 = time.time()
    out = build(args.database_url)
    print(f"{'entity':28s}{'raw':>10s}{'staged':>10s}{'flagged':>10s}")
    for line in out.splitlines():
        e, r, s, f = line.split("|")
        print(f"{e:28s}{int(r):>10,}{int(s):>10,}{int(f):>10,}")
    print(f"staging built in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
