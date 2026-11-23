"""The Monday run:  python -m agent.run --asof 2026-10-26

   1. score everyone renewing in 26 weeks as of that Monday (scoring.score; stored with full inputs in step 4)
   2. diff against the previous Monday's stored scores
   3. draft CSM notes and customer emails into the approval queue (status 'pending'; the agent NEVER sends anything)
   4. record the run

Safe to repeat: the same Monday replaces its own rows and cannot duplicate a draft (unique key + the 14-day dedupe), and drafts a human
has already approved or rejected are never touched. Everything the agent writes, the week's scores included, goes in ONE transaction,
so a failed run leaves the previous Monday as the latest one the UI shows. A failure is written to agent.run as status 'failed' with
the error, and the process exits non-zero: it is never silent.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback

import pandas as pd

from features import features
from features.build_features import load_tables
from resolve.io import read_df
from scoring import score as sc
from . import diff, drafts
from .db import ensure_schema, lit, psql, tx

DEFAULT_MODEL = "model/artifacts/model.pkl"


def run_id_for(asof: pd.Timestamp) -> str:
    return f"agent-{asof:%Y%m%d}"


STORED_COLS = ["run_id", "tenant_id", "account_id", "owner_id", "csm_name", "csm_active", "account_name", "segment", "renewal_date",
               "renewal_source", "weeks_to_renewal", "risk", "priority", "band", "rank_in_csm", "data_note", "alerts", "drivers", "protective"]


def load_stored(db_url: str, score_run_id: str) -> pd.DataFrame:
    df = read_df(db_url, f"""select run_id, tenant_id, account_id, owner_id, csm_name, csm_active, account_name, segment, renewal_date::text renewal_date,
        renewal_source, weeks_to_renewal, risk, priority, band, rank_in_csm, data_note, alerts::text alerts, drivers::text drivers,
        protective::text protective from score.account_score where run_id = {lit(score_run_id)}""")
    return _parse_stored(df)


def stored_from_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """What load_stored would return once `rows` (scoring.score.store_rows) were written: the same CSV-of-text round trip, without the database."""
    text = pd.read_csv(io.StringIO(rows[STORED_COLS].to_csv(index=False)), dtype=str, keep_default_na=False, na_values=[""])
    return _parse_stored(text)


def _parse_stored(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("weeks_to_renewal", "risk", "priority"):
        df[c] = pd.to_numeric(df[c])
    df["rank_in_csm"] = pd.to_numeric(df.rank_in_csm).astype(int)
    df["csm_active"] = df.csm_active.eq("t")
    df["data_note"] = df.data_note.fillna("")
    for c in ("alerts", "drivers", "protective"):
        df[c] = df[c].map(lambda s: json.loads(s) if isinstance(s, str) else [])
    return df


def previous_run(db_url: str, asof: pd.Timestamp) -> tuple[str | None, str | None]:
    out = psql(db_url, f"select run_id || '|' || model_sha from score.run where asof < '{asof:%Y-%m-%d} 00:00:00+00' "
                     "order by asof desc, created_at desc, run_id desc limit 1")     # same choice as the UI's latest run (ui/server.py LATEST)
    return tuple(out.split("|")) if out else (None, None)


def existing_drafts(db_url: str, asof: pd.Timestamp) -> set:
    lo, hi = asof - pd.Timedelta(days=drafts.DEDUPE_DAYS), f"{asof:%Y-%m-%d} 06:00:00+00"      # pending/approved/sent inside the window block a new draft
    # superseded drafts still count: they were live drafts at that time, and a rerun of an old Monday must reproduce the same queue.
    # Drafts a HUMAN rejected do not block (the reviewer said no to that text, not to ever hearing about the account again).
    df = read_df(db_url, f"""select tenant_id, account_id, kind from agent.draft
        where (status in ('pending','approved','sent') or decided_by = 'agent:superseded')
        and created_at > '{lo:%Y-%m-%d} 06:00:00+00' and created_at <= '{hi}'""")
    return set(zip(df.tenant_id, df.account_id, df.kind))


def contact_maps(db_url: str, asof: pd.Timestamp):
    c = read_df(db_url, "select tenant_id, account_id, first_name, email, is_champion, is_decision_maker from core.contact")
    b = read_df(db_url, f"""select distinct tenant_id, account_id, lower(bounced_address) a from core.email
                            where is_bounce and bounced_address is not null and known_at <= '{asof:%Y-%m-%d} 06:00:00+00'""")
    contacts = {k: g for k, g in c.groupby(["tenant_id", "account_id"])}
    bounced = b.groupby(["tenant_id", "account_id"]).a.apply(set).to_dict() if len(b) else {}
    return contacts, bounced


def _csv_block(table: str, df: pd.DataFrame, cols: list[str]) -> str:
    return f"\\copy {table} ({', '.join(cols)}) from stdin with (format csv, header)\n{df[cols].to_csv(index=False)}\\.\n"


def write_all(db_url: str, rid: str, asof: pd.Timestamp, score_run_id: str, n_scored: int, started: str,
              changes: pd.DataFrame, new_drafts: pd.DataFrame, score_script: str = "") -> None:
    """score_script (scoring.score.persist_script) goes in the same transaction, so a failed run publishes no new scores either."""
    ch = changes.copy()
    ch.insert(0, "run_id", rid)
    ch["detail"] = ch.detail.map(json.dumps)
    ccols = ["run_id", "tenant_id", "account_id", "kind", "from_band", "to_band", "from_risk", "to_risk", "detail"]
    body = [f"""insert into agent.run (run_id, asof, score_run_id, started_at, status, n_scored)
        values ({lit(rid)}, '{asof:%Y-%m-%d} 06:00:00+00', {lit(score_run_id)}, '{started}', 'failed', {n_scored})
        on conflict (run_id) do update set asof = excluded.asof, score_run_id = excluded.score_run_id, started_at = excluded.started_at,
            status = 'failed', n_scored = excluded.n_scored, error = null, finished_at = null""",
            f"delete from agent.change where run_id = {lit(rid)}"]
    body[0] += ";"
    body[1] += ";"
    script = score_script + "\n".join(body) + "\n"
    if len(ch):                                       # COPY into a forced-RLS table is refused by Postgres: stage, then INSERT
        script += ("create temp table _c (run_id text, tenant_id text, account_id text, kind text, from_band text, to_band text, "
                   "from_risk double precision, to_risk double precision, detail jsonb) on commit drop;\n")
        script += _csv_block("_c", ch, ccols)
        script += f"insert into agent.change ({', '.join(ccols)}) select {', '.join(ccols)} from _c;\n"
    if len(new_drafts):
        d = new_drafts.copy()
        d.insert(0, "run_id", rid)
        d["reasons"] = d.reasons.map(json.dumps)
        dcols = ["run_id", "tenant_id", "account_id", "owner_id", "kind", "to_address", "subject", "body", "reasons", "created_at"]
        script += ("create temp table _d (run_id text, tenant_id text, account_id text, owner_id text, kind text, to_address text, "
                   "subject text, body text, reasons jsonb, created_at timestamptz) on commit drop;\n")
        script += _csv_block("_d", d, dcols)
        # a fresh draft replaces an UNREVIEWED one that has gone stale (older than the dedupe window); reviewed drafts are never touched
        script += (f"update agent.draft x set status = 'rejected', decided_by = 'agent:superseded', decided_at = now() from _d "
                   f"where x.status = 'pending' and x.tenant_id = _d.tenant_id and x.account_id = _d.account_id and x.kind = _d.kind "
                   f"and x.run_id <> {lit(rid)} and x.created_at <= '{asof:%Y-%m-%d} 06:00:00+00'::timestamptz - interval '{drafts.DEDUPE_DAYS} days';\n")
        script += ("insert into agent.draft (run_id, tenant_id, account_id, owner_id, kind, to_address, subject, body, reasons, created_at)\n"
                   "select run_id, tenant_id, account_id, owner_id, kind, to_address, subject, body, reasons, created_at from _d\n"
                   "on conflict (run_id, tenant_id, account_id, kind) do nothing;\n")
    script += (f"update agent.run set status = 'ok', finished_at = now(), "
               f"n_changes = (select count(*) from agent.change where run_id = {lit(rid)}), "
               f"n_drafts = (select count(*) from agent.draft where run_id = {lit(rid)}) where run_id = {lit(rid)};\n")
    tx(db_url, script)


def record_failure(db_url: str, asof: pd.Timestamp, error: str) -> None:
    ensure_schema(db_url)
    rid = run_id_for(asof)
    psql(db_url, f"""insert into agent.run (run_id, asof, status, finished_at, error) values ({lit(rid)}, '{asof:%Y-%m-%d} 06:00:00+00', 'failed', now(), {lit(error[-2000:])})
        on conflict (run_id) do update set status = 'failed', finished_at = now(), error = excluded.error""")


def execute(db_url: str, asof: pd.Timestamp, model_path: str = DEFAULT_MODEL) -> dict:
    started = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S+00")
    ensure_schema(db_url)
    psql(db_url, sc.DDL)
    model = sc.load_model(model_path)
    pop = sc.population(db_url, asof)
    scored = sc.score(pop, asof, model, features.prepare(load_tables(db_url)))
    score_run_id = sc.run_id_for(asof, model)
    rows = sc.store_rows(score_run_id, scored)
    stored = stored_from_rows(rows)                    # scores are written below, with everything else, in one transaction

    prev_id, prev_sha = previous_run(db_url, asof)
    prev = load_stored(db_url, prev_id) if prev_id else None
    changes = diff.compute(prev, stored, same_model=(prev_sha == model["sha"]))
    contacts, bounced = contact_maps(db_url, asof)
    new = drafts.generate(stored, changes, contacts, bounced, existing_drafts(db_url, asof), asof)
    write_all(db_url, run_id_for(asof), asof, score_run_id, len(stored), started, changes, new,
              score_script=sc.persist_script(score_run_id, asof, model, rows))
    return {"run_id": run_id_for(asof), "score_run_id": score_run_id, "previous": prev_id, "n_scored": len(stored),
            "n_changes": len(changes), "n_new_drafts": len(new)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--asof", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    asof = pd.Timestamp(a.asof)
    t0 = time.time()
    try:
        r = execute(a.database_url, asof, a.model)
    except Exception as e:                                   # recorded, then loud
        try:
            record_failure(a.database_url, asof, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        finally:
            print(f"agent run for {a.asof} FAILED: {e}", file=sys.stderr)
        return 1
    print(f"{r['run_id']}: scored {r['n_scored']:,}, {r['n_changes']} changes vs {r['previous'] or 'nothing (baseline)'}, "
          f"{r['n_new_drafts']} new drafts queued, in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
