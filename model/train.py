"""Train the churn-risk model and backtest it the way it will be used.   DATABASE_URL=... python -m model.train

Backtest = walk-forward by renewal date: for each quarter Q, fit on renewals that completed BEFORE Q starts, score Q's
snapshots. Never a random split: rows from one renewal are near-duplicates, and the future must not inform the past.

Production model: sign-log counts -> standardise -> L2 logistic regression. It is within noise of gradient boosting on this
data, and every score decomposes into per-feature contributions a CSM can be shown (model/explain.py). Boosting is kept as a
reported challenger so the choice is evidence, not taste.
"""
from __future__ import annotations

import argparse
import io
import os
import pickle
import sys
import warnings
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from resolve.io import read_df
from .design import KEYS, design, numeric_columns, signlog

warnings.filterwarnings("ignore")
FOLDS = ["2025-04-14", "2025-10-13", "2026-04-13"]          # test windows start here, each runs 6 months
END = "2027-01-01"
HORIZONS = (4, 8, 12, 16, 24)


def make_logit(C=0.03):
    return make_pipeline(SimpleImputer(strategy="median"), FunctionTransformer(signlog), StandardScaler(),
                         LogisticRegression(C=C, max_iter=4000))


def make_boost():
    return HistGradientBoostingClassifier(max_depth=2, learning_rate=0.04, max_iter=300, l2_regularization=5,
                                          min_samples_leaf=400, random_state=0)


def load(db_url: str) -> pd.DataFrame:
    D = read_df(db_url, "select t.*, a.owner_id from feat.training t join core.account a using (tenant_id, account_id)")
    for c in D.columns:
        if c not in ("tenant_id", "account_id", "snap_at", "renewal_date", "outcome", "segment", "owner_id"):
            D[c] = pd.to_numeric(D[c])
    D["renewal_date"] = pd.to_datetime(D.renewal_date)
    D["y"] = D.outcome.isin(["churned", "downgraded"]).astype(int)
    return D


def calibration_table(y, p, bins=(0, .1, .2, .3, .4, .5, 1.01)):
    b = pd.cut(p, bins, right=False)
    t = pd.DataFrame({"y": y, "p": p, "b": b}).groupby("b", observed=True).agg(n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    return t


def ece(y, p, n=10):
    b = pd.qcut(p, n, duplicates="drop")
    g = pd.DataFrame({"y": y, "p": p, "b": b}).groupby("b", observed=True).agg(n=("y", "size"), p=("p", "mean"), y=("y", "mean"))
    return float((g.n * (g.p - g.y).abs()).sum() / g.n.sum())


def top_share(y, p, share=.1):
    k = max(1, int(len(p) * share))
    idx = np.argsort(-p)[:k]
    return y[idx].mean(), y[idx].sum() / max(y.sum(), 1)


def per_csm_precision(df, p, k=10, horizon=8):
    """The product metric: if each CSM looked only at their 10 riskiest accounts renewing in the window, how many are real?"""
    d = df.assign(p=p)
    d = d[(d.k_weeks - horizon).abs() < 1]
    d = d.drop_duplicates(["tenant_id", "account_id", "renewal_date"])
    hits, shown, base, caught, total = 0, 0, [], 0, 0
    for _, g in d.groupby(["tenant_id", "owner_id"]):
        if len(g) < k:
            continue
        top = g.nlargest(k, "p")
        hits += top.y.sum(); shown += len(top); base.append(g.y.mean()); caught += top.y.sum(); total += g.y.sum()
    return hits / max(shown, 1), float(np.mean(base)) if base else np.nan, caught / max(total, 1)


def backtest(D: pd.DataFrame, out) -> dict:
    X_all = design(D)
    cols = list(X_all.columns)
    results = {}
    print("WALK-FORWARD BACKTEST (train on renewals completed before the fold; test on the next 6 months)", file=out)
    print(f"{'fold':>12s} {'train':>7s} {'test':>7s} {'base':>6s} | {'model AUC':>9s} {'PR-AUC':>7s} {'Brier':>7s} {'ECE':>6s} | {'boost AUC':>9s} | {'rule AUC':>8s}", file=out)
    allp = []
    for f0 in FOLDS:
        t0 = pd.Timestamp(f0)
        t1 = min(t0 + pd.DateOffset(months=6), pd.Timestamp(END))
        tr, te = D.renewal_date < t0, (D.renewal_date >= t0) & (D.renewal_date < t1)
        m = make_logit().fit(X_all[tr], D.y[tr]); p = m.predict_proba(X_all[te])[:, 1]
        pb = make_boost().fit(X_all[tr], D.y[tr]).predict_proba(X_all[te])[:, 1]
        rule = D.loc[te, "tk_90"].fillna(0).values + 2 * D.loc[te, "tk_cancel_180"].fillna(0).values      # "tickets and cancellation requests", no model
        y = D.y[te].values
        print(f"{f0:>12s} {tr.sum():>7,} {te.sum():>7,} {y.mean():>6.3f} | {roc_auc_score(y, p):>9.3f} {average_precision_score(y, p):>7.3f} "
              f"{brier_score_loss(y, p):>7.4f} {ece(y, p):>6.3f} | {roc_auc_score(y, pb):>9.3f} | {roc_auc_score(y, rule):>8.3f}", file=out)
        allp.append(pd.DataFrame({"idx": D.index[te], "p": p, "pb": pb, "rule": rule, "fold": f0}))
    P = pd.concat(allp).set_index("idx")
    T = D.loc[P.index].join(P)
    y, p = T.y.values, T.p.values
    results["auc"] = roc_auc_score(y, p)
    print(f"\n  pooled over all folds: AUC {results['auc']:.3f}, PR-AUC {average_precision_score(y, p):.3f} (base rate {y.mean():.3f}), "
          f"Brier {brier_score_loss(y, p):.4f} vs {brier_score_loss(y, np.full_like(p, y.mean())):.4f} for always guessing the base rate", file=out)

    print("\nBY HORIZON (how far before the renewal the snapshot was taken; further out = less is observable yet)", file=out)
    print(f"  {'weeks out':>10s}{'model':>8s}{'boost':>8s}{'rule':>8s}{'top10% precision':>18s}{'top10% captures':>17s}", file=out)
    for k in HORIZONS:
        mk = (T.k_weeks - k).abs() < 1
        prec, cap = top_share(T.y[mk].values, T.p[mk].values)
        print(f"  {k:>10d}{roc_auc_score(T.y[mk], T.p[mk]):>8.3f}{roc_auc_score(T.y[mk], T.pb[mk]):>8.3f}{roc_auc_score(T.y[mk], T.rule[mk]):>8.3f}"
              f"{prec:>18.1%}{cap:>17.1%}", file=out)
        results[f"auc_k{k}"] = roc_auc_score(T.y[mk], T.p[mk])

    print("\nBY TENANT AND SEGMENT (a single global model is an assumption: does it hold for each?)", file=out)
    for col in ("tenant_id", "segment"):
        for v, g in T.groupby(col):
            mk = (g.k_weeks - 8).abs() < 1
            g8 = g[mk]
            print(f"  {col[:6]:>6s} {str(v):12s} n={len(g8):>5,} base {g8.y.mean():.3f}  AUC@8w {roc_auc_score(g8.y, g8.p):.3f}   "
                  f"mean predicted {g8.p.mean():.3f}", file=out)

    print("\nCALIBRATION at 8 weeks out (does 'a 30% risk' mean 30%?)", file=out)
    mk = (T.k_weeks - 8).abs() < 1
    ct = calibration_table(T.y[mk].values, T.p[mk].values)
    for b, r in ct.iterrows():
        print(f"  predicted {str(b):14s} n={int(r.n):>5,}   mean predicted {r.predicted:.3f}   actual {r.actual:.3f}", file=out)
    results["ece8"] = ece(T.y[mk].values, T.p[mk].values)
    print(f"  expected calibration error {results['ece8']:.3f}", file=out)

    print("\nTHE PRODUCT METRIC: each CSM's top 10 accounts renewing in the window, 8 weeks out", file=out)
    prec, base, cap = per_csm_precision(T, T.p.values)
    precr, _, capr = per_csm_precision(T, T.rule.values)
    precb, _, capb = per_csm_precision(T, T.pb.values)
    print(f"  precision in the CSM's top 10: model {prec:.1%}  boost {precb:.1%}  rule {precr:.1%}  random (avg CSM base rate) {base:.1%}", file=out)
    print(f"  share of all at-risk renewals that land in some CSM's top 10: model {cap:.1%}  rule {capr:.1%}", file=out)
    results["csm_top10_precision"], results["csm_base"] = prec, base
    return results


def fit_final(D: pd.DataFrame, train_through: pd.Timestamp):
    tr = D.renewal_date < train_through
    X = design(D[tr])
    model = make_logit().fit(X, D.y[tr])
    return model, list(X.columns)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--out", default="model/BACKTEST.txt")
    ap.add_argument("--artifact", default="model/artifacts/model.pkl")
    ap.add_argument("--train-through", default="2026-10-13", help="renewals completed before this date are used to fit the final model")
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    D = load(a.database_url)
    buf = io.StringIO()
    print(f"training table: {len(D):,} snapshots, {D.groupby(['tenant_id','account_id','renewal_date']).ngroups:,} labelled renewals, "
          f"{D.y.mean():.1%} churned or downgraded, renewals {D.renewal_date.min().date()} to {D.renewal_date.max().date()}\n", file=buf)
    res = backtest(D, buf)
    model, cols = fit_final(D, pd.Timestamp(a.train_through))
    Path(a.artifact).parent.mkdir(parents=True, exist_ok=True)
    with open(a.artifact, "wb") as fh:
        pickle.dump({"model": model, "columns": cols, "trained_through": a.train_through, "metrics": res}, fh)
    print(f"\nfinal model: {len(cols)} inputs, fitted on renewals before {a.train_through} -> {a.artifact}", file=buf)
    text = buf.getvalue()
    print(text)
    Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
