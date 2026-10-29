"""Score every account with an upcoming renewal, as of a given Monday, and store the result with everything needed to trace it.

    DATABASE_URL=... python -m scoring.score --asof 2026-10-26

Population: accounts still customers at `asof` whose next renewal falls within HORIZON_WEEKS. Renewal dates come from CRM renewal
opportunities; beyond the last opportunity the next anniversary is PROJECTED (flagged as such). Features use only facts known at `asof`.
Every row persists the model version, the full feature vector and the reasons, so any number on a CSM's screen can be reproduced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from features import features
from features.build_features import load_tables
from model.design import design
from model.explain import explain
from resolve.io import read_df, write_df
from ingest.load_raw import psql
from .alerts import alerts_for

HORIZON_WEEKS = 26
MIN_WEEKS, MAX_WEEKS = 4, 26           # the range the model was trained on; nearer renewals are scored as if 4 weeks out
RED, YELLOW = 0.30, 0.18               # calibrated risk. ~12% of accounts are red: about what a CSM can personally work (see DECISIONS D10)
RUN_HOUR = 6


def band(p: float) -> str:
    return "red" if p >= RED else "yellow" if p >= YELLOW else "green"


def load_model(path: str) -> dict:
    raw = Path(path).read_bytes()
    d = pickle.loads(raw)
    d["sha"] = hashlib.md5(raw).hexdigest()[:10]
    return d


def population(db_url: str, asof: pd.Timestamp) -> pd.DataFrame:
    ren = read_df(db_url, "select tenant_id, account_id, renewal_date::text renewal_date, outcome from core.renewal")
    ren["renewal_date"] = pd.to_datetime(ren.renewal_date)
    acc = read_df(db_url, "select tenant_id, account_id, segment, owner_id, csm_name, csm_active, created_at, name from core.account")
    acc["created_at"] = pd.to_datetime(acc.created_at, utc=True, format="ISO8601").dt.tz_convert(None)
    acc["csm_active"] = acc.csm_active.eq("t")
    last = ren.groupby(["tenant_id", "account_id"]).renewal_date.max().reset_index()
    proj = last.assign(renewal_date=last.renewal_date + pd.DateOffset(years=1), outcome="projected")
    allr = pd.concat([ren.assign(src="crm"), proj.assign(src="projected")], ignore_index=True)
    allr = allr.sort_values(["tenant_id", "account_id", "renewal_date"])
    allr["renewal_number"] = allr.groupby(["tenant_id", "account_id"]).cumcount() + 1
    churned_before = ren[(ren.outcome == "churned") & (ren.renewal_date <= asof.normalize())][["tenant_id", "account_id"]].drop_duplicates()
    nxt = allr[allr.renewal_date > asof.normalize()].sort_values("renewal_date").groupby(["tenant_id", "account_id"], as_index=False).first()
    nxt = nxt.merge(churned_before.assign(gone=1), how="left", on=["tenant_id", "account_id"])
    nxt = nxt[nxt.gone.isna()].drop(columns="gone")
    nxt = nxt[nxt.renewal_date <= asof.normalize() + pd.Timedelta(weeks=HORIZON_WEEKS)]
    pop = nxt.merge(acc, on=["tenant_id", "account_id"])
    pop = pop[pop.created_at <= asof]
    return pop.rename(columns={"src": "renewal_source"}).reset_index(drop=True)


def score(pop: pd.DataFrame, asof: pd.Timestamp, model: dict, prepared: dict) -> pd.DataFrame:
    snaps = pop[["tenant_id", "account_id", "renewal_date", "segment", "renewal_number"]].copy()
    snaps["snap_at"] = asof.normalize() + pd.Timedelta(hours=RUN_HOUR)
    feats = features.compute(snaps, prepared)
    feats["weeks_to_renewal"] = feats["k_weeks"]
    feats["k_weeks"] = feats["k_weeks"].clip(MIN_WEEKS, MAX_WEEKS)
    X = design(feats, model["columns"])[model["columns"]]
    p = model["model"].predict_proba(X)[:, 1]
    why = explain(model, feats)
    out = pop.merge(feats[["tenant_id", "account_id"]].assign(
        risk=p, weeks_to_renewal=feats.weeks_to_renewal.values), on=["tenant_id", "account_id"])
    out["alerts"] = [alerts_for(prepared, t, a, asof) for t, a in zip(out.tenant_id, out.account_id)]
    # priority = what the CSM list is sorted by: the model risk, lifted to the floor of any event alert. `risk` itself is never altered.
    out["priority"] = [max([r] + [x["floor"] for x in al]) for r, al in zip(out.risk, out.alerts)]
    out["band"] = [band(x) for x in out.priority]
    out["drivers"], out["protective"], out["concept_effects"] = why.drivers.values, why.protective.values, why.concept_effects.values
    out["features"] = feats.drop(columns=["tenant_id", "account_id", "snap_at", "renewal_date", "segment", "outcome"], errors="ignore").to_dict("records")
    activity = feats[["p_b0", "p_b1", "p_b2", "tk_90", "em_in_90", "em_out_90"]].sum(axis=1)
    ever = feats[["has_product", "has_tickets", "has_email"]].sum(axis=1)
    out["data_note"] = np.where(ever.values == 0, "no_data_in_any_source",
                                np.where((activity.values == 0) & (feats.age_days.values > 120), "silent_for_90_days", ""))
    out["rank_in_csm"] = out.groupby(["tenant_id", "owner_id"]).priority.rank(ascending=False, method="first").astype(int)
    return out.sort_values(["tenant_id", "owner_id", "rank_in_csm"]).reset_index(drop=True)


DDL = """
create schema if not exists score;
create table if not exists score.run (run_id text primary key, asof timestamptz not null, model_sha text not null,
    trained_through text, n_scored int, created_at timestamptz default now());
create table if not exists score.account_score (
    run_id text not null references score.run, tenant_id text not null, account_id text not null, renewal_date date,
    renewal_source text, weeks_to_renewal double precision, risk double precision, band text, rank_in_csm int,
    priority double precision, alerts jsonb, owner_id text, csm_name text, csm_active boolean, data_note text, account_name text, segment text,
    drivers jsonb, protective jsonb, concept_effects jsonb, features jsonb,
    primary key (run_id, tenant_id, account_id));
alter table score.account_score add column if not exists priority double precision;
alter table score.account_score add column if not exists alerts jsonb;
create index if not exists account_score_owner on score.account_score (run_id, tenant_id, owner_id, rank_in_csm)
"""


def persist(db_url: str, asof: pd.Timestamp, model: dict, scored: pd.DataFrame) -> str:
    run_id = f"{asof:%Y%m%d}-{model['sha']}"
    psql(db_url, DDL)
    psql(db_url, f"delete from score.account_score where run_id = '{run_id}'; delete from score.run where run_id = '{run_id}'")
    psql(db_url, f"insert into score.run (run_id, asof, model_sha, trained_through, n_scored) values "
                 f"('{run_id}', '{asof:%Y-%m-%d} {RUN_HOUR:02d}:00+00', '{model['sha']}', '{model['trained_through']}', {len(scored)})")
    d = pd.DataFrame({
        "run_id": run_id, "tenant_id": scored.tenant_id, "account_id": scored.account_id,
        "renewal_date": scored.renewal_date.dt.strftime("%Y-%m-%d"), "renewal_source": scored.renewal_source,
        "weeks_to_renewal": scored.weeks_to_renewal.round(2), "risk": scored.risk.round(5), "priority": scored.priority.round(5), "alerts": scored.alerts.map(json.dumps), "band": scored.band,
        "rank_in_csm": scored.rank_in_csm, "owner_id": scored.owner_id, "csm_name": scored.csm_name,
        "csm_active": scored.csm_active.map({True: "t", False: "f"}), "data_note": scored.data_note,
        "account_name": scored["name"], "segment": scored.segment,
        "drivers": scored.drivers.map(json.dumps), "protective": scored.protective.map(json.dumps),
        "concept_effects": scored.concept_effects.map(json.dumps),
        "features": scored.features.map(lambda r: json.dumps({k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in r.items()}))})
    write_df(db_url, "score.account_score", d, "select 1")
    return run_id


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD, the Monday this run represents")
    ap.add_argument("--model", default="model/artifacts/model.pkl")
    ap.add_argument("--no-persist", action="store_true")
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    t0 = time.time()
    asof = pd.Timestamp(a.asof)
    model = load_model(a.model)
    pop = population(a.database_url, asof)
    scored = score(pop, asof, model, features.prepare(load_tables(a.database_url)))
    run_id = None if a.no_persist else persist(a.database_url, asof, model, scored)
    print(f"as of {a.asof}: scored {len(scored):,} accounts renewing within {HORIZON_WEEKS} weeks "
          f"({(scored.renewal_source == 'projected').sum()} on projected dates) in {time.time() - t0:.1f}s; run {run_id}")
    print("bands: " + ", ".join(f"{b} {int((scored.band == b).sum())}" for b in ("red", "yellow", "green")),
          f"| low-data flags: {int((scored.data_note != '').sum())} | accounts whose CSM has left: {int((~scored.csm_active).sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
