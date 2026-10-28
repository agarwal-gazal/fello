"""Score entity resolution against the generator's answer key (data/ground_truth/entity_links.csv).

    DATABASE_URL=... python -m resolve.evaluate --data data [--out resolve/EVALUATION.txt]

The pipeline never reads the answer key; this file is the only place it is used, and only to MEASURE.
Metrics per source entity:
    coverage   share of real-customer records that received a link
    precision  share of links that point at the right account        <- the number that matters most: a wrong link
                                                                        poisons that account's features
    recall     share of real-customer records linked to the right account
Noise records (test-mode customers, internal QA workspaces/mail) have no true account; linking one is a false positive.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

from .io import read_df


def load(db_url: str, data: Path):
    truth = pd.read_csv(data / "ground_truth" / "entity_links.csv", dtype=str).drop_duplicates()
    acc = pd.read_csv(data / "ground_truth" / "accounts_truth.csv", dtype=str)
    planted = acc[acc.planted_role.notna()][["true_account_id", "planted_role"]]
    link = read_df(db_url, "select tenant_id, source_entity, source_record_id, account_id, method, confidence, conflict, reason from er.link")
    link["conflict"] = link["conflict"].eq("t")
    link["confidence"] = link["confidence"].astype(float)
    # canonical account (a Salesforce Id) -> the true account it really is
    sf_truth = truth[truth.source_entity == "salesforce_account"].set_index(["tenant_id", "source_record_id"]).true_account_id.to_dict()
    return truth, acc, planted, link, sf_truth


def report(db_url: str, data: Path) -> None:
    truth, acc, planted, link, sf_truth = load(db_url, data)
    link["pred_true"] = [sf_truth.get((t, a)) if isinstance(a, str) else None for t, a in zip(link.tenant_id, link.account_id)]
    j = truth.merge(link, on=["tenant_id", "source_entity", "source_record_id"], how="left", validate="one_to_one",
                    suffixes=("", "_l"))
    print("=" * 100)
    print("ENTITY RESOLUTION: evaluation against the answer key")
    print("=" * 100)

    # ------------------------------------------------------------------ Salesforce de-duplication
    sf = j[j.source_entity == "salesforce_account"].copy()
    n_true = sf.true_account_id.nunique()
    n_pred = sf.account_id.nunique()
    def pairs(col):
        g = sf.groupby(col).size()
        return int((g * (g - 1) // 2).sum())
    both = sf.groupby(["true_account_id", "account_id"]).size()
    tp = int((both * (both - 1) // 2).sum())
    print(f"\n[1] Salesforce de-duplication: {len(sf):,} records, {n_true:,} real companies -> {n_pred:,} accounts predicted")
    print(f"    pairwise precision {tp / max(pairs('account_id'), 1):.4f}   pairwise recall {tp / max(pairs('true_account_id'), 1):.4f}"
          f"   (true duplicate pairs {pairs('true_account_id')}, found {tp}, wrongly merged {pairs('account_id') - tp})")
    missed = sf[sf.duplicated("true_account_id", keep=False)].groupby("true_account_id").account_id.nunique()
    print(f"    duplicate groups left split: {(missed > 1).sum()}")

    # ------------------------------------------------------------------ per entity
    print("\n[2] Links by source entity (noise = test-mode / internal records, which must NOT be linked)")
    print(f"    {'entity':20s}{'real':>8s}{'linked':>9s}{'coverage':>10s}{'precision':>11s}{'recall':>8s}{'noise':>7s}{'noise linked':>14s}")
    rows = []
    for ent in ("stripe_customer", "zendesk_org", "product_workspace", "zendesk_ticket", "gmail_message"):
        e = j[j.source_entity == ent]
        real, noise = e[e.true_account_id != "NOISE"], e[e.true_account_id == "NOISE"]
        linked = real[real.account_id.notna()]
        correct = linked[linked.pred_true == linked.true_account_id]
        print(f"    {ent:20s}{len(real):>8,}{len(linked):>9,}{len(linked) / len(real):>10.1%}{len(correct) / max(len(linked), 1):>11.2%}"
              f"{len(correct) / len(real):>8.1%}{len(noise):>7,}{int(noise.account_id.notna().sum()):>14,}")
        rows.append((ent, linked, correct))

    # ------------------------------------------------------------------ by method
    print("\n[3] Precision by method (which evidence can be trusted?)")
    print(f"    {'entity':20s}{'method':28s}{'links':>8s}{'precision':>11s}{'conflicts':>11s}")
    for ent, linked, correct in rows:
        for m, g in linked.groupby("method"):
            ok = (g.pred_true == g.true_account_id).mean()
            print(f"    {ent:20s}{m:28s}{len(g):>8,}{ok:>11.2%}{int(g.conflict.sum()):>11,}")

    # ------------------------------------------------------------------ errors
    print("\n[4] Wrong links (all of them, up to 12 per entity)")
    twin = acc[acc.name_twin_of.notna()][["true_account_id", "name_twin_of"]]
    twin_pairs = set(zip(twin.true_account_id, twin.name_twin_of)) | set(zip(twin.name_twin_of, twin.true_account_id))
    for ent, linked, correct in rows:
        wrong = linked[linked.pred_true != linked.true_account_id]
        tw = sum((a, b) in twin_pairs for a, b in zip(wrong.true_account_id, wrong.pred_true))
        print(f"    {ent}: {len(wrong)} wrong ({tw} onto a name-twin of the right account)")
        for r in wrong.head(12).itertuples():
            print(f"        {r.source_record_id:>16s} true={r.true_account_id} pred={r.pred_true} via {r.method} ({r.confidence:.2f}){' CONFLICT' if r.conflict else ''}")

    # ------------------------------------------------------------------ why unlinked
    print("\n[5] Why real records are NOT linked (this is the honest coverage gap)")
    for ent in ("stripe_customer", "zendesk_org", "product_workspace", "zendesk_ticket", "gmail_message"):
        e = j[(j.source_entity == ent) & (j.true_account_id != "NOISE") & j.account_id.isna()]
        if len(e):
            print(f"    {ent:20s} {len(e):>5,} unlinked: " + ", ".join(f"{k}={v}" for k, v in e.reason.value_counts().items()))
    gm = j[(j.source_entity == "gmail_message") & (j.true_account_id == "NOISE")]
    print(f"    gmail noise (newsletters, internal): {len(gm):,} messages, {int(gm.account_id.notna().sum())} wrongly linked")

    # ------------------------------------------------------------------ twins
    print(f"\n[6] Name twins (different companies with near-identical names): {len(twin)} pairs in the data")
    sf_wrong = sf[sf.pred_true.fillna("") != sf.true_account_id] if "pred_true" in sf else None
    merged_twins = sum(1 for a, b in zip(twin.true_account_id, twin.name_twin_of)
                       if (sf[sf.true_account_id == a].account_id.isin(sf[sf.true_account_id == b].account_id)).any())
    print(f"    twin pairs falsely merged in Salesforce: {merged_twins}")

    # ------------------------------------------------------------------ planted
    print("\n[7] The planted cases: are all their signals attributed to the right account?")
    for p in planted.itertuples():
        mine = j[(j.true_account_id == p.true_account_id) & (j.source_entity != "salesforce_account")]
        line = []
        for ent, g in mine.groupby("source_entity"):
            line.append(f"{ent.replace('_', ' ')} {(g.pred_true == g.true_account_id).sum()}/{len(g)}")
        print(f"    {p.planted_role:26s} {p.true_account_id}: " + "; ".join(line))
    lost = j[j.true_account_id.isin(planted[planted.planted_role.str.startswith('lost')].true_account_id)
             & (j.source_entity == 'zendesk_ticket')]
    print("    lost-renewal tickets by link method: " + str(lost.method.value_counts(dropna=False).to_dict()))

    # ------------------------------------------------------------------ accounts with no data
    pop = j[j.true_account_id != "NOISE"]
    n_acc = acc.true_account_id.nunique()
    good = pop[pop.pred_true == pop.true_account_id]
    ent_cov = good[good.source_entity != "salesforce_account"].groupby("source_entity").true_account_id.nunique()
    print(f"\n[8] Accounts (of {n_acc:,}) with at least one correctly attributed record per source: " +
          ", ".join(f"{k}={v:,}" for k, v in ent_cov.items()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None, help="also write the report to this file")
    args = ap.parse_args(argv)
    if not args.database_url:
        sys.exit("set DATABASE_URL or pass --database-url")
    buf = io.StringIO()
    with redirect_stdout(buf):
        report(args.database_url, Path(args.data))
    text = buf.getvalue()
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
