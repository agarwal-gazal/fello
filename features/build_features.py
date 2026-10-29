"""Build the training table of weekly-style snapshots.   DATABASE_URL=... python -m features.build_features

One row per (labelled renewal, k weeks before it), for k in KS. The label is whether that renewal churned or downgraded >= 25%.
Rows from the same renewal are highly correlated, so evaluation must split by renewal DATE, never by row (see model/).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ingest.load_raw import psql
from resolve.io import read_df, write_df
from . import features

KS = list(range(4, 27, 2))                     # snapshot 4, 6, ... 26 weeks before the renewal
MIN_OBSERVED_DAYS = 28                         # cold-start guard: the warehouse must have been collecting for >= 4 weeks
TABLES = {
    "product_event": "select tenant_id, account_id, event_name, user_id, event_ts, known_at, api_status from core.product_event",
    "ticket": "select tenant_id, account_id, created_at, known_at, solved_at, priority, category, is_reopened, first_reply_minutes, csat from core.ticket",
    "email": "select tenant_id, account_id, sent_at, known_at, direction, is_bounce, bounced_address, is_auto_reply from core.email",
    "billing_event": "select tenant_id, account_id, event_at, known_at, event_type, quantity, previous_quantity from core.billing_event",
    "invoice": "select tenant_id, account_id, created_at, known_at, status from core.invoice",
    "account": "select tenant_id, account_id, segment, employees, created_at from core.account",
    "contact": "select tenant_id, account_id, email, is_champion from core.contact",
}


def load_tables(db_url: str) -> dict:
    return {k: read_df(db_url, sql) for k, sql in TABLES.items()}


def training_snapshots(db_url: str, window_start: str) -> pd.DataFrame:
    r = read_df(db_url, """select r.tenant_id, r.account_id, r.renewal_date::text renewal_date, r.renewal_number, r.outcome,
                                  a.segment, a.created_at from core.renewal r join core.account a using (tenant_id, account_id)
                           where r.is_label""")
    r["renewal_date"] = pd.to_datetime(r.renewal_date)
    r["created_at"] = pd.to_datetime(r.created_at, utc=True, format="ISO8601").dt.tz_convert(None)
    r["renewal_number"] = r.renewal_number.astype(int)
    r["y"] = r.outcome.isin(["churned", "downgraded"]).astype(int)
    parts = []
    for k in KS:
        s = r.copy()
        s["snap_at"] = s.renewal_date - pd.Timedelta(weeks=k) + pd.Timedelta(hours=6)       # Monday-morning style run time
        parts.append(s)
    s = pd.concat(parts, ignore_index=True)
    ok = (s.snap_at >= pd.Timestamp(window_start) + pd.Timedelta(days=MIN_OBSERVED_DAYS)) & (s.snap_at >= s.created_at)
    return s[ok].drop(columns=["created_at"]).reset_index(drop=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--data", default="data")
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    window_start = json.loads((Path(a.data) / "manifest.json").read_text())["window_start"]
    t0 = time.time()
    snaps = training_snapshots(a.database_url, window_start)
    tables = load_tables(a.database_url)
    feats = features.compute(snaps, tables)
    feats["snap_at"] = feats.snap_at.dt.strftime("%Y-%m-%d %H:%M:%S+00")
    feats["renewal_date"] = feats.renewal_date.dt.strftime("%Y-%m-%d")
    num = [c for c in feats.columns if c not in ("tenant_id", "account_id", "snap_at", "renewal_date", "outcome", "segment")]
    ddl = ("drop schema if exists feat cascade; create schema feat; create table feat.training (tenant_id text, account_id text, "
           "snap_at timestamptz, renewal_date date, outcome text, segment text, " + ", ".join(f"{c} double precision" for c in num) + ")")
    cols = ["tenant_id", "account_id", "snap_at", "renewal_date", "outcome", "segment"] + num
    write_df(a.database_url, "feat.training", feats[cols], ddl)
    psql(a.database_url, "create index on feat.training (tenant_id, account_id, renewal_date)")
    print(f"{len(feats):,} snapshots from {feats.groupby(['tenant_id','account_id','renewal_date']).ngroups:,} renewals, "
          f"{len(num)} numeric columns, churn+downgrade rate {feats.y.mean():.3f}, in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
