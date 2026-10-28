"""Sanity checks on the generated data. Uses ground truth, so it is an evaluation of the
GENERATOR (is the data realistic and does it carry signal?), not part of the pipeline.

    python -m data_gen.validate --data data
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


def load(raw: Path, src: str, ent: str) -> pd.DataFrame:
    frames = []
    for tdir in sorted(raw.iterdir()):
        p = tdir / src / f"{ent}.jsonl"
        if p.exists():
            df = pd.read_json(p, lines=True, dtype=False)
            df["tenant_id"] = tdir.name
            frames.append(df)
    return pd.concat(frames, ignore_index=True)


def ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce", format="ISO8601")


def section(title: str):
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    args = ap.parse_args(argv)
    root = Path(args.data)
    raw, truth = root / "raw", root / "ground_truth"
    acc = pd.read_csv(truth / "accounts_truth.csv")
    ren = pd.read_csv(truth / "renewals_truth.csv", parse_dates=["renewal_date"])
    links = pd.read_csv(truth / "entity_links.csv", dtype=str)
    manifest = json.load(open(root / "manifest.json"))

    sf_acc = load(raw, "salesforce", "accounts")
    sf_con = load(raw, "salesforce", "contacts")
    st_cus = load(raw, "stripe", "customers")
    st_ev = load(raw, "stripe", "events")
    zd_t = load(raw, "zendesk", "tickets")
    pe = load(raw, "product", "events")
    gm = load(raw, "gmail", "messages")

    # ------------------------------------------------------------------ volumes
    section("Volumes")
    print(f"accounts: {len(acc):,}   event rows: {manifest['event_rows']:,}   all rows: {manifest['total_rows']:,}")
    print(acc.groupby("tenant_id").size().to_string())

    # ------------------------------------------------------------------ outcomes
    section("Renewal outcomes (resolved)")
    res = ren[ren.outcome != "pending"]
    print(res.outcome.value_counts(normalize=True).round(3).to_string())
    print("churn or downgrade by tenant:")
    print(res.groupby("tenant_id").churned_or_downgraded.mean().round(3).to_string())
    pend = ren[ren.outcome == "pending"]
    print(f"pending renewals: {len(pend)}  (hidden: {pend.hidden_outcome_if_pending.value_counts().to_dict()})")
    act = acc[acc.end_date.isna()]
    print(f"active accounts: {len(act):,}")

    # ------------------------------------------------------------------ mess rates
    section("Mess actually present")
    n = len(acc)
    dup = (sf_acc.Type == "Prospect").sum()
    print(f"SF duplicate account records:        {dup} ({dup / n:.1%})")
    print(f"SF websites missing:                 {sf_acc.Website.isna().mean():.1%}")
    print(f"SF contacts with no email:           {sf_con.Email.isna().mean():.1%}")
    meta_has = st_cus.metadata.apply(lambda m: bool(m)).groupby(st_cus.tenant_id).mean().round(2)
    print(f"Stripe customers carrying SF id:     {meta_has.to_dict()}")
    print(f"Stripe test-mode customers:          {(~st_cus.livemode).sum()}")
    print(f"Stripe duplicate webhook event ids:  {st_ev.duplicated('id').sum()} ({st_ev.duplicated('id').mean():.1%})")
    print(f"Zendesk tickets w/o organisation:    {zd_t.organization_id.isna().mean():.1%}")
    zdup = zd_t.duplicated(["tenant_id", "id"]).sum()
    print(f"Zendesk duplicate ticket versions:   {zdup} ({zdup / len(zd_t):.1%})   "
          f"[ticket ids also collide ACROSS tenants: {zd_t.duplicated('id').sum() - zdup} rows]")
    pe_ts, pe_sy = ts(pe.timestamp), ts(pe._synced_at)
    lag_d = (pe_sy - pe_ts).dt.total_seconds() / 86400
    print(f"Product events late >1d:             {(lag_d > 1).mean():.1%}   (>14d backfill: {(lag_d > 14).mean():.1%})")
    print(f"Product events duplicated:           {pe.duplicated('event_id').mean():.1%}")
    print(f"Product events with naive timestamp: {(~pe.timestamp.str.endswith('Z')).mean():.1%}")
    print(f"Product events future-dated vs sync: {(pe_ts > pe_sy).mean():.2%}")
    print(f"Product events missing workspace:    {pe.workspace_id.isna().mean():.1%}")
    gm_bounce = gm.snippet.str.contains("couldn't be delivered", na=False).sum()
    print(f"Gmail bounce messages:               {gm_bounce}")
    print(f"Gmail rows landing in bulk backfill: {(ts(gm._synced_at) - pd.to_datetime(gm.internalDate.astype('int64'), unit='ms', utc=True) > pd.Timedelta(days=14)).mean():.1%}")

    # ------------------------------------------------------------------ sparsity
    section("Data richness per account (rows across product+tickets+gmail)")
    ws = links[links.source_entity == "product_workspace"].set_index("source_record_id").true_account_id
    zl = links[links.source_entity == "zendesk_org"]
    zo = pd.Series(zl.true_account_id.values, index=(zl.tenant_id + ":" + zl.source_record_id).values)
    pe["true_id"] = pe.workspace_id.map(ws)
    zd_t["true_id"] = (zd_t.tenant_id + ":" + zd_t.organization_id.astype("Int64").astype(str)).map(zo)
    dom = {}
    for _, r in acc.iterrows():
        dom[r.domain] = r.true_account_id
        if isinstance(r.alias_domain, str) and r.alias_domain:
            dom[r.alias_domain] = r.true_account_id
    miss = zd_t.true_id.isna()
    zd_t.loc[miss, "true_id"] = zd_t.loc[miss, "requester_email"].str.split("@").str[-1].map(dom)

    def hdr(m, name):
        for h in m["headers"]:
            if h["name"] == name:
                return h["value"]
        return ""
    gm["frm"] = gm.payload.apply(lambda p: hdr(p, "From"))
    gm["to"] = gm.payload.apply(lambda p: hdr(p, "To"))
    gm["cust_addr"] = np.where(gm.labelIds.apply(lambda l: "INBOX" in l), gm.frm.str.extract(r"<(.+)>")[0], gm.to.str.split(",").str[0])
    gm["true_id"] = gm.cust_addr.str.split("@").str[-1].map(dom)
    gm = gm[~gm.true_id.isna()].copy()

    rows = pd.concat([pe.true_id.dropna(), zd_t.true_id.dropna(), gm.true_id]).value_counts()
    acc["rows"] = acc.true_account_id.map(rows).fillna(0)
    print(acc.groupby("segment").rows.describe(percentiles=[.1, .5, .9]).round(1).to_string())
    print(f"accounts with <5 total activity rows: {(acc.rows < 5).mean():.1%}   (zero: {(acc.rows == 0).mean():.1%})")
    print(f"missing a whole source: product {acc.missing_sources.fillna('').str.contains('product').mean():.1%}, "
          f"zendesk {acc.missing_sources.fillna('').str.contains('zendesk').mean():.1%}")

    # ------------------------------------------------------------------ signal vs outcome
    section("Does the data carry signal? AUC of simple as-of features (R - 60d) vs churn/downgrade")
    st_cus_map = links[links.source_entity == "stripe_customer"].set_index("source_record_id").true_account_id
    st_ev["cus"] = st_ev.data.apply(lambda d: d["object"].get("customer"))
    st_ev["true_id"] = st_ev.cus.map(st_cus_map)
    fails = st_ev[(st_ev.type == "invoice.payment_failed")].drop_duplicates("id")
    pe["t"], zd_t["t"] = pe_ts, ts(zd_t.created_at)
    gm["t"] = pd.to_datetime(gm.internalDate.astype("int64"), unit="ms", utc=True)
    gm["inbound"] = gm.labelIds.apply(lambda l: "INBOX" in l)
    gm = gm[~gm.snippet.str.contains("couldn't be delivered", na=False)]
    fails = fails.assign(t=pd.to_datetime(fails.created, unit="s", utc=True))

    by = {k: g for k, g in pe.dropna(subset=["true_id"]).groupby("true_id")}
    byz = {k: g for k, g in zd_t.dropna(subset=["true_id"]).groupby("true_id")}
    byg = {k: g for k, g in gm.groupby("true_id")}
    byf = {k: g for k, g in fails.dropna(subset=["true_id"]).groupby("true_id")}
    seg = acc.set_index("true_account_id").segment
    feats = []
    d = pd.Timedelta
    for _, r in res[res.renewal_date >= "2024-04-01"].iterrows():
        a_t = pd.Timestamp(r.renewal_date, tz="UTC") - d(days=60)
        tid = r.true_account_id
        p, z, g, f = by.get(tid), byz.get(tid), byg.get(tid), byf.get(tid)
        u1 = 0 if p is None else ((p.t > a_t - d(days=56)) & (p.t <= a_t)).sum()
        u0 = 0 if p is None else ((p.t > a_t - d(days=112)) & (p.t <= a_t - d(days=56))).sum()
        if p is None:
            base_rate, last_prod = 0.0, 365
        else:
            hist = p[(p.t > a_t - d(days=365)) & (p.t <= a_t - d(days=56))]
            base_rate = len(hist) / ((365 - 56) / 56)
            seen = p[p.t <= a_t]
            last_prod = min(365, (a_t - seen.t.max()).days) if len(seen) else 365
        tk = 0 if z is None else ((z.t > a_t - d(days=90)) & (z.t <= a_t)).sum()
        bad = 0 if z is None else ((z.t > a_t - d(days=180)) & (z.t <= a_t) & z.tags.apply(lambda x: any(c in x for c in ("cancellation", "performance", "bug")))).sum()
        fp = 0 if f is None else ((f.t > a_t - d(days=180)) & (f.t <= a_t)).sum()
        if g is not None:
            gi = g[(g.t <= a_t) & g.inbound]
            days_since = (a_t - gi.t.max()).days if len(gi) else 365
            inb = ((g.t > a_t - d(days=90)) & (g.t <= a_t) & g.inbound).sum()
        else:
            days_since, inb = 365, 0
        feats.append(dict(true_account_id=tid, y=r.churned_or_downgraded, segment=seg[tid],
                          renewal_date=r.renewal_date, true_health=r.pre_renewal_health,
                          usage_drop=np.log((u1 + 1) / (u0 + 1)), usage_recent=u1, usage_prior=u0,
                          usage_vs_baseline=np.log((u1 + 1) / (base_rate + 1)), days_since_product=last_prod,
                          tickets_90d=tk, bad_tickets_180d=bad, failed_payments_180d=fp,
                          days_since_inbound=days_since, inbound_90d=inb))
    F = pd.DataFrame(feats)
    print(f"resolved renewals evaluated: {len(F):,}  (positive rate {F.y.mean():.1%})")
    for name, sub in (("all accounts", F), ("mid + enterprise", F[F.segment != "smb"]),
                      ("accounts with >=6 product events in prior 112d", F[(F.usage_recent + F.usage_prior) >= 6])):
        out = []
        for c in ("usage_drop", "tickets_90d", "bad_tickets_180d", "failed_payments_180d", "days_since_inbound", "inbound_90d"):
            if sub[c].nunique() > 1 and sub.y.nunique() > 1:
                a = roc_auc_score(sub.y, sub[c])
                out.append(f"{c}: {a:.2f}")
        print(f"  [{name}, n={len(sub)}]  " + "  ".join(out))
    print("  (0.50 = no signal. Values <0.5 mean 'higher value => LESS risk', as expected for usage_drop and inbound_90d.)")

    # ceiling vs. what a plain model recovers
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    cols = ["usage_drop", "usage_vs_baseline", "days_since_product", "tickets_90d", "bad_tickets_180d",
            "failed_payments_180d", "days_since_inbound", "inbound_90d"]
    F["seg_mid"], F["seg_ent"] = (F.segment == "mid").astype(int), (F.segment == "ent").astype(int)
    X = cols + ["seg_mid", "seg_ent"]
    tr, te = F[F.renewal_date < "2025-07-01"], F[F.renewal_date >= "2025-07-01"]
    lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=1.0)).fit(tr[X], tr.y)
    auc_lr = roc_auc_score(te.y, lr.predict_proba(te[X])[:, 1])
    auc_true = roc_auc_score(te.y, -te.true_health)
    te_me = te[te.segment != "smb"]
    print(f"\n  CEILING (knowing hidden pre-renewal health):       test AUC {auc_true:.3f}")
    print(f"  Plain logistic regression on 8 as-of features:     test AUC {auc_lr:.3f}   (train {len(tr):,} / test {len(te):,})")
    print(f"  ...mid+enterprise only:  ceiling {roc_auc_score(te_me.y, -te_me.true_health):.3f}   "
          f"logreg {roc_auc_score(te_me.y, lr.predict_proba(te_me[X])[:, 1]):.3f}")

    # ------------------------------------------------------------------ pipeline hazards
    section("Point-in-time hazards present in the raw data")
    cutoff = pd.Timestamp(manifest["export_at"])
    for name, df in (("product", pe), ("zendesk", zd_t), ("stripe_events", st_ev), ("gmail", gm)):
        s_ = ts(df._synced_at)
        assert (s_ <= cutoff).all(), f"{name}: record synced after the export cutoff"
    print("no raw record is synced after the cutoff: OK")
    pw = pe.dropna(subset=["t"]).copy()
    pw["wk_end"] = pw.t.dt.tz_convert(None).dt.to_period("W-SUN").dt.end_time.dt.ceil("D").dt.tz_localize("UTC")
    pw["visible_mon_6am"] = ts(pw._synced_at) <= pw.wk_end + pd.Timedelta(hours=6)
    by_w = pw.groupby("wk_end").visible_mon_6am.mean().iloc[1:-2]
    print(f"share of a week's product events already in the warehouse at 6am the next Monday: "
          f"mean {by_w.mean():.1%}, worst week {by_w.min():.1%}")
    print("  -> features computed 'as of Monday 06:00' under-count the week that just ended; a model trained on "
          "event time (hindsight) but served on arrival time would be mis-calibrated.")
    early = gm[gm.t < pd.Timestamp(manifest["window_start"], tz="UTC") + pd.Timedelta(days=200)]
    lag_gm = (ts(early._synced_at) - early.t).dt.days
    print(f"Gmail: {(lag_gm > 14).mean():.0%} of early-window messages were synced >14d after they were sent "
          "(mailbox connected late -> bulk backfill).")
    owner = sf_acc[sf_acc.Type == "Customer"].groupby("OwnerId").size()
    users = load(raw, "salesforce", "users").set_index("Id")
    act_owner = owner.reindex(users.index).fillna(0)
    print(f"active accounts per CSM: median {act_owner[users.IsActive].median():.0f} "
          f"(min {act_owner[users.IsActive].min():.0f}, max {act_owner[users.IsActive].max():.0f}); "
          f"accounts still owned by an inactive CSM: {int(act_owner[~users.IsActive].sum())}")

    section("How hard are the cross-system joins? (naive approaches)")
    sfa = sf_acc[sf_acc.Type != "Prospect"]
    nm = sfa.Name.str.lower()
    st_names = st_cus[st_cus.livemode].name.str.lower()
    print(f"Stripe->Salesforce via metadata.salesforce_id:   {st_cus[st_cus.livemode].metadata.apply(lambda m: bool(m)).mean():.0%}")
    print(f"Stripe->Salesforce via exact (lower-cased) name: {st_names.isin(set(nm)).mean():.0%}")
    zd_org = load(raw, "zendesk", "organizations")
    print(f"Zendesk org->Salesforce via external_id:         {zd_org.external_id.notna().mean():.0%}")
    print(f"Zendesk org->Salesforce via exact name:          {zd_org.name.str.lower().isin(set(nm)).mean():.0%}")
    wsd = load(raw, "product", "workspaces")
    print(f"Product workspace->Salesforce via crm_account_id:{wsd.crm_account_id.notna().mean():.0%}")
    twins = acc[acc.name_twin_of.notna()]
    print(f"near-identical company names that are DIFFERENT companies (fuzzy-match traps): {len(twins)} pairs")
    print(f"accounts with an alias email domain:             {acc.alias_domain.notna().mean():.1%}")
    cold = ((pd.Timestamp('2026-10-27') - pd.to_datetime(acc.contract_start)).dt.days < 100)
    print(f"cold-start accounts (<100 days old):             {cold.mean():.1%}")

    # ------------------------------------------------------------------ planted
    section("Planted cases: 4-week buckets leading to the renewal (product events | tickets | inbound emails)")
    planted = json.load(open(truth / "planted_cases.json"))
    for pc in planted:
        tid, R = pc["true_account_id"], pd.Timestamp(pc["renewal_date"], tz="UTC")
        p, z, g = by.get(tid), byz.get(tid), byg.get(tid)

        def buckets(df):
            if df is None:
                return [0] * 13
            return [int(((df.t > R - d(weeks=4 * (i + 1))) & (df.t <= R - d(weeks=4 * i))).sum()) for i in range(13)][::-1]
        gi = None if g is None else g[g.inbound]
        print(f"{pc['role']:26s} {tid}  renews {pc['renewal_date']}  outcome={pc['outcome']}  pre-renewal health={pc['pre_renewal_health']}")
        print(f"   product  (t-52w..t-0): {buckets(p)}")
        print(f"   tickets               : {buckets(z)}")
        print(f"   inbound emails        : {buckets(gi)}")
        print(f"   built-in mess         : {pc['built_in_mess']}")
    print()


if __name__ == "__main__":
    main()
