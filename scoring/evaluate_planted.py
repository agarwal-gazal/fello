"""Would the system have caught the two renewals the VP lost, and how early? (uses the answer key only to find the accounts)

    DATABASE_URL=... python -m scoring.evaluate_planted --data data [--train-through 2026-04-13]

The model is refit ONLY on renewals completed before --train-through (before either lost renewal), then each Monday from 26 weeks
out is scored exactly as production would: features from facts known that morning, ranked among the CSM's other accounts renewing
in the next 26 weeks.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from features import features
from features.build_features import load_tables
from model import train
from resolve.io import read_df
from .score import HORIZON_WEEKS, band, population, score


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--data", default="data")
    ap.add_argument("--train-through", default="2026-04-13")
    ap.add_argument("--out", default="scoring/PLANTED_CASES.txt")
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    D = train.load(a.database_url)
    pipe, cols = train.fit_final(D, pd.Timestamp(a.train_through))
    model = {"model": pipe, "columns": cols, "trained_through": a.train_through, "sha": "backtest"}
    prepared = features.prepare(load_tables(a.database_url))

    truth = Path(a.data) / "ground_truth"
    planted = pd.read_csv(truth / "accounts_truth.csv", dtype=str)
    planted = planted[planted.planted_role.notna()][["true_account_id", "planted_role"]]
    links = pd.read_csv(truth / "entity_links.csv", dtype=str)
    sf = links[links.source_entity == "salesforce_account"].rename(columns={"source_record_id": "sf_id"})
    m = read_df(a.database_url, "select tenant_id, sf_id, account_id from er.sf_merge").merge(sf, on=["tenant_id", "sf_id"])
    amap = m[["tenant_id", "account_id", "true_account_id"]].drop_duplicates().merge(planted, on="true_account_id")
    ren = read_df(a.database_url, "select tenant_id, account_id, renewal_date::text renewal_date, outcome from core.renewal")
    ren["renewal_date"] = pd.to_datetime(ren.renewal_date)

    lines = [f"PLANTED CASES: weekly as-of scoring, model trained only on renewals completed before {a.train_through}",
             f"bands: red >= {0.30:.2f}, yellow >= {0.18:.2f} calibrated risk;  list = the CSM's accounts renewing in the next {HORIZON_WEEKS} weeks\n"]
    for r in amap.itertuples():
        rr = ren[(ren.tenant_id == r.tenant_id) & (ren.account_id == r.account_id) & (ren.renewal_date >= pd.Timestamp("2026-05-01")) &
                 (ren.renewal_date <= pd.Timestamp("2026-10-26"))].sort_values("renewal_date")
        if rr.empty:
            continue
        R, outcome = rr.iloc[0].renewal_date, rr.iloc[0].outcome
        rows = []
        for wk in range(HORIZON_WEEKS, 1, -1):
            asof = R - pd.Timedelta(weeks=wk)
            asof = asof - pd.Timedelta(days=asof.weekday())          # snap back to that week's Monday
            pop = population(a.database_url, asof)
            me = pop[(pop.tenant_id == r.tenant_id) & (pop.account_id == r.account_id)]
            if me.empty:
                continue
            owner = me.iloc[0].owner_id
            sc = score(pop[(pop.tenant_id == r.tenant_id) & (pop.owner_id == owner)], asof, model, prepared)
            row = sc[sc.account_id == r.account_id].iloc[0]
            rows.append((wk, asof.date(), row.priority, row.band, int(row.rank_in_csm), len(sc), (row.alerts[0]["text"] if row.alerts else (row.drivers[0]["text"] if row.drivers else ""))))
        df = pd.DataFrame(rows, columns=["weeks_out", "asof", "risk", "band", "rank", "list_size", "top_reason"])
        lines.append(f"{r.planted_role}  ({r.true_account_id})  renewal {R.date()}  outcome: {outcome}")
        lines.append(f"  {'weeks out':>9s} {'as of':>11s} {'prior.':>6s} {'band':>7s} {'rank in CSM list':>17s}  top reason or alert")
        for x in df.itertuples():
            lines.append(f"  {x.weeks_out:>9d} {x.asof!s:>11s} {x.risk:>6.2f} {x.band:>7s} {f'{x.rank} of {x.list_size}':>17s}  {x.top_reason[:80]}")
        if outcome == "churned":
            # earliest week from which it is red/top-10 at EVERY later weekly run (a stable alert, not a lucky blip)
            def stable(cond):
                ok = df.assign(c=cond).sort_values("weeks_out", ascending=False)
                w = None
                for x in ok.itertuples():
                    if not x.c:
                        w = None
                    elif w is None:
                        w = x.weeks_out
                return w
            lines.append(f"  -> stably in the CSM's top 10 from {stable(df['rank'] <= 10)} weeks out; stably red from {stable(df.band == 'red')} weeks out")
        else:
            lines.append(f"  -> weeks spent red: {(df.band == 'red').sum()} of {len(df)}; best rank {df['rank'].min()}, worst {df['rank'].max()}")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
