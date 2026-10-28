"""Entry point:  python -m data_gen.generate --out data --seed 7

Layout written:
  <out>/raw/<tenant_id>/<source>/<entity>.jsonl     what the pipeline is allowed to see
  <out>/ground_truth/*                              hidden truth, for evaluation ONLY
  <out>/manifest.json                               row counts, seed, window
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np

from .config import EXPORT_AT, N_WEEKS, TENANTS, WINDOW_START
from .names import NameFactory
from .sources_activity import emit_gmail, emit_product, emit_zendesk, make_calendar
from .sources_crm import emit_salesforce, emit_stripe
from .world import build_tenant, finish_accounts, week_date


def write_jsonl(path: Path, records: list, rng) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    order = rng.permutation(len(records))            # deliver out of order
    with open(path, "w") as f:
        for i in order:
            f.write(json.dumps(records[int(i)], separators=(",", ":"), default=str) + "\n")
    return len(records)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", type=float, default=1.0, help="multiplies the number of accounts")
    ap.add_argument("--usage-scale", type=float, default=1.0,
                    help="multiplies product-event volume (default keeps the whole dataset near ~200k events)")
    args = ap.parse_args(argv)
    from . import sources_activity
    sources_activity.USAGE_SCALE[0] = args.usage_scale

    out = Path(args.out)
    raw, truth = out / "raw", out / "ground_truth"
    truth.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    kids = np.random.SeedSequence(args.seed).spawn(len(TENANTS) * 6 + 1)
    names = NameFactory(np.random.default_rng(kids[0]))
    manifest = {"seed": args.seed, "window_start": WINDOW_START.isoformat(), "export_at": EXPORT_AT.isoformat(),
                "n_weeks": N_WEEKS, "tenants": {}}

    all_accs, links = [], []
    for ti, t in enumerate(TENANTS):
        rngs = [np.random.default_rng(kids[1 + ti * 6 + j]) for j in range(6)]
        accs = build_tenant(t, rngs[0], names, args.scale)
        csms = finish_accounts(t, accs, rngs[0], names)
        cal = make_calendar(t, rngs[0])
        all_accs += accs
        counts = {}
        sf = emit_salesforce(t, accs, csms, rngs[1], links)
        st = emit_stripe(t, accs, rngs[2], links)
        zd = emit_zendesk(t, accs, rngs[3], links, cal)
        pr = emit_product(t, accs, rngs[4], links, cal)
        gm = emit_gmail(t, accs, csms, rngs[5], links, cal)
        for src, data in (("salesforce", sf), ("stripe", st), ("zendesk", zd), ("product", pr), ("gmail", gm)):
            for entity, recs in data.items():
                n = write_jsonl(raw / t.tenant_id / src / f"{entity}.jsonl", recs, rngs[1])
                counts[f"{src}.{entity}"] = n
        manifest["tenants"][t.tenant_id] = {"name": t.name, "accounts": len(accs), "csms": len(csms),
                                            "rows": counts}
        print(f"  {t.tenant_id}: {len(accs)} accounts, {sum(counts.values()):,} rows", flush=True)

    write_truth(truth, all_accs, links)
    # "events" = row-level activity records (excludes CRM entities / reference tables)
    ev_keys = ("product.events", "gmail.messages", "zendesk.tickets", "stripe.invoices", "stripe.events")
    manifest["event_rows"] = sum(v for tt in manifest["tenants"].values() for k, v in tt["rows"].items() if k in ev_keys)
    manifest["total_rows"] = sum(v for tt in manifest["tenants"].values() for v in tt["rows"].values())
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"done in {time.time() - t0:.1f}s  | event rows: {manifest['event_rows']:,}  | all rows: {manifest['total_rows']:,}")


def write_truth(truth: Path, accs, links):
    with open(truth / "accounts_truth.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["true_account_id", "tenant_id", "name", "domain", "alias_domain", "segment", "industry",
                    "seats_initial", "unit_price", "billing", "contract_start", "end_date", "archetype",
                    "activity_mult", "missing_sources", "champion_left_week", "csm_id", "planted_role",
                    "name_twin_of", "sf_id", "sf_dup_id", "stripe_customer_id", "zendesk_org_id", "workspace_ids"])
        for a in accs:
            w.writerow([a.true_id, a.tenant_id, a.legal, a.domain, a.alias_domain or "", a.segment, a.industry,
                        a.seats0, a.unit_price, a.billing, a.contract_start, a.end_date or "", a.archetype,
                        round(a.act_mult, 3), "|".join(sorted(a.missing_sources)),
                        "" if a.champion_left_week is None else a.champion_left_week, a.csm_id, a.role,
                        a.twin_of, a.sf_id, a.sf_dup_id, a.stripe_cus, a.zd_org_id or "", "|".join(a.workspaces)])
    with open(truth / "renewals_truth.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["true_account_id", "tenant_id", "renewal_number", "renewal_date", "outcome",
                    "hidden_outcome_if_pending", "churned_or_downgraded", "pre_renewal_health", "p_churn_true",
                    "exogenous_churn", "cancel_notice_date", "seats_before", "seats_after"])
        for a in accs:
            for r in a.renewals:
                bad = r.hidden_outcome in ("churned", "downgraded")
                w.writerow([a.true_id, a.tenant_id, r.k, r.day, r.outcome,
                            r.hidden_outcome if r.outcome == "pending" else "",
                            int(bad) if r.outcome != "pending" else "", round(r.hp, 4), round(r.p_churn, 4),
                            int(r.exogenous), r.notice_day or "", r.seats_before, r.seats_after])
    with gzip.open(truth / "health_weekly.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["true_account_id", "week", "week_start", "health"])
        from .config import N_PRE
        for a in accs:
            for wk in range(a.first_week(), a.last_week() + 1):
                w.writerow([a.true_id, wk, week_date(wk), round(float(a.h[N_PRE + wk]), 4)])
    with open(truth / "entity_links.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tenant_id", "source_entity", "source_record_id", "true_account_id"])
        w.writerows(links)
    planted = []
    by_id = {}
    for a in accs:
        if a.role:
            r = next(r for r in a.renewals if r.day == a.planted_renewal)
            planted.append({
                "true_account_id": a.true_id, "tenant_id": a.tenant_id, "role": a.role,
                "renewal_date": a.planted_renewal.isoformat(), "outcome": r.outcome,
                "pre_renewal_health": round(r.hp, 3), "built_in_mess": list(a.planted_mess), "note": a.planted_note,
                "source_ids": {"salesforce": a.sf_id, "stripe_customer": a.stripe_cus, "zendesk_org": a.zd_org_id or None,
                               "product_workspaces": a.workspaces},
                "expectation": ("Should be ranked in a CSM's top ~10 at least 8 weeks before the renewal date."
                                if a.role.startswith("lost") else
                                "Should NOT be treated as high risk with high confidence at renewal time."
                                if a.role == "decoy_quiet_renewed" else
                                "Hard negative: may be flagged mid-decline, but outcome is 'renewed'.")})
    (truth / "planted_cases.json").write_text(json.dumps(planted, indent=2))


if __name__ == "__main__":
    main()
