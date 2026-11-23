"""Point-in-time features.

    compute(snapshots, tables) -> DataFrame

A snapshot is (tenant, account, snap_at, renewal_date). Every feature for it is computed from facts with
    event_time <= snap_at   AND   known_at <= snap_at
The second condition is the whole point: a record that happened before the snapshot but reached the warehouse after it
(Gmail bulk backfill, late product events, Stripe retries) must NOT be visible, otherwise the model trains on information
production will not have on Monday morning.  tests/test_features.py proves it: appending late data cannot move an earlier snapshot.

Deliberately NOT features: CRM ARR / contract end date (current-state snapshots, 25% stale, overwritten after churn),
Salesforce opportunity stage (the outcome), Stripe cancel_at_period_end (the customer's own cancellation notice; the CSM
already sees it and a model living on it is not an early-warning system), ticket 'status' as of today.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DAY = np.timedelta64(1, "D")
CAP = 365.0


def _days(a, b):                 # a - b in days, broadcast
    return (a - b) / DAY


def _flag(s: pd.Series) -> np.ndarray:
    return np.asarray(s.astype(str).str.lower().isin(["t", "true"]))


def _acct_arrays(df: pd.DataFrame, ts: str) -> dict:
    out = {}
    if df is None or df.empty:
        return out
    for key, g in df.sort_values(ts).groupby(["tenant_id", "account_id"], sort=False):
        out[key] = g
    return out


def _acct_columns(df: pd.DataFrame, ts: str, cols: dict) -> dict:
    """(tenant, account) -> {name: numpy array}. Every column is parsed ONCE for the whole table, then sliced per account:
    the per-account pandas work this replaces was most of the run time."""
    if df is None or df.empty:
        return {}
    df = df.sort_values(ts)
    whole = {"t": df[ts].values.astype("datetime64[ns]"), "kn": df["known_at"].values.astype("datetime64[ns]"),
             **{k: np.asarray(f(df)) for k, f in cols.items()}}
    return {key: {k: v[ix] for k, v in whole.items()} for key, ix in df.groupby(["tenant_id", "account_id"], sort=False).indices.items()}


def _num(s: pd.Series) -> np.ndarray:
    return np.asarray(pd.to_numeric(s, errors="coerce"), dtype=float)


def prepare(tables: dict) -> dict:
    """Index the fact tables by (tenant, account) once; datetimes as tz-naive UTC numpy."""
    def norm(df, cols):
        df = df.copy()
        for c in cols:
            df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").dt.tz_convert(None)
        return df
    product = norm(tables["product_event"], ["event_ts", "known_at"])
    ticket = norm(tables["ticket"], ["created_at", "known_at", "solved_at"])
    email = norm(tables["email"], ["sent_at", "known_at"])
    billing = norm(tables["billing_event"], ["event_at", "known_at"])
    p = {
        "product": _acct_arrays(product, "event_ts"),          # DataFrames per account: scoring/alerts.py reads these
        "ticket": _acct_arrays(ticket, "created_at"),
        "email": _acct_arrays(email, "sent_at"),
        "billing": _acct_arrays(billing, "event_at"),
        "invoice": _acct_arrays(norm(tables["invoice"], ["created_at", "known_at"]), "created_at"),
        "cols": {                                              # numpy columns per account: compute() reads these
            "product": _acct_columns(product, "event_ts", {
                "uid": lambda d: d.user_id.fillna(""), "api": lambda d: _num(d.api_status), "name": lambda d: d.event_name}),
            "ticket": _acct_columns(ticket, "created_at", {
                "hi": lambda d: d.priority.isin(["high", "urgent"]), "cancel": lambda d: d.category.values == "cancellation",
                "outage": lambda d: d.category.isin(["outage", "performance", "bug"]), "bad_csat": lambda d: d.csat.values == "bad",
                "reopened": lambda d: _flag(d.is_reopened), "reply": lambda d: _num(d.first_reply_minutes)}),
            "email": _acct_columns(email, "sent_at", {
                "inbound": lambda d: (d.direction.values == "inbound") & ~_flag(d.is_auto_reply) & ~_flag(d.is_bounce),
                "outbound": lambda d: d.direction.values == "outbound", "bounce": lambda d: _flag(d.is_bounce),
                "auto": lambda d: _flag(d.is_auto_reply), "bounced": lambda d: d.bounced_address.fillna("").str.lower()}),
            "billing": _acct_columns(billing, "event_at", {
                "pf": lambda d: d.event_type.values == "invoice.payment_failed",
                "drop": lambda d: (d.event_type.values == "customer.subscription.updated") & (_num(d.quantity) < _num(d.previous_quantity))}),
        },
    }
    acc = tables["account"].copy()
    acc["created_at"] = pd.to_datetime(acc["created_at"], utc=True, format="ISO8601").dt.tz_convert(None)
    p["account"] = acc.set_index(["tenant_id", "account_id"])
    p["account_info"] = dict(zip(p["account"].index, zip(acc.created_at, acc.employees)))
    ct = tables["contact"]
    champs = ct[ct.is_champion.astype(str).str.lower().isin(["t", "true", "1"])]
    p["champion_emails"] = champs.groupby(["tenant_id", "account_id"]).email.apply(lambda s: set(s.dropna().str.lower())).to_dict()
    return p


def _ages(S, ev):
    """Age in days of every event at every snapshot (n_snap, n_event), and whether the warehouse knew of it yet. Once per source."""
    return _days(S[:, None], ev["t"][None, :]), ev["kn"][None, :] <= S[:, None]


def _window(age, known, lo, hi):
    """Mask (n_snap, n_event): event in (S-hi, S-lo] days AND already known at S."""
    return (age > lo) & (age <= hi) & known


NB = 9                                  # 4-week buckets going back 36 weeks


def _buckets(age, known, extra=None, prefix="x"):
    """Event counts in consecutive 28-day buckets before the snapshot (b0 = most recent), plus the account's own long-run
    baseline (buckets 4-8) and recent-vs-baseline ratio. Gives the model the shape of the trajectory, not one summary number."""
    cols = {}
    for j in range(NB):
        m = _window(age, known, 28 * j, 28 * (j + 1))
        if extra is not None:
            m = m & extra[None, :]
        cols[f"{prefix}_b{j}"] = m.sum(1).astype(float)
    recent = (cols[f"{prefix}_b0"] + cols[f"{prefix}_b1"] + cols[f"{prefix}_b2"]) / 3
    base = sum(cols[f"{prefix}_b{j}"] for j in range(3, NB)) / (NB - 3)
    cols[f"{prefix}_recent_vs_base"] = (recent + .5) / (base + .5)
    cols[f"{prefix}_mid_vs_base"] = ((cols[f"{prefix}_b2"] + cols[f"{prefix}_b3"] + cols[f"{prefix}_b4"]) / 3 + .5) / (base + .5)
    return cols


def _no_buckets(n, prefix):
    """What _buckets gives an account with no events of this kind."""
    return {**{f"{prefix}_b{j}": np.zeros(n) for j in range(NB)}, f"{prefix}_recent_vs_base": np.ones(n), f"{prefix}_mid_vs_base": np.ones(n)}


def compute(snaps: pd.DataFrame, tables_or_prepared: dict) -> pd.DataFrame:
    P = tables_or_prepared if "champion_emails" in tables_or_prepared else prepare(tables_or_prepared)
    snaps = snaps.copy()
    snaps["snap_at"] = pd.to_datetime(snaps["snap_at"], utc=True).dt.tz_convert(None)
    snaps["renewal_date"] = pd.to_datetime(snaps["renewal_date"])
    # one group per (tenant, account), in order of first appearance, rows in their original order: what groupby(sort=False) gives
    codes = pd.factorize(snaps["tenant_id"].astype(str) + "\x00" + snaps["account_id"].astype(str))[0]
    order = np.argsort(codes, kind="stable")
    groups = np.split(order, np.flatnonzero(np.diff(codes[order])) + 1)
    snap_at, renewal = snaps["snap_at"].values.astype("datetime64[ns]"), snaps["renewal_date"].values.astype("datetime64[ns]")
    tenants, accounts = snaps["tenant_id"].values, snaps["account_id"].values
    feats = [_account(P, tenants[ix[0]], accounts[ix[0]], snap_at[ix], renewal[ix]) for ix in groups]
    front = ["tenant_id", "account_id", "snap_at", "renewal_date"]
    out = snaps.iloc[np.concatenate(groups)][front + [c for c in snaps.columns if c not in front]].reset_index(drop=True)
    return pd.concat([out, pd.DataFrame({c: np.concatenate([f[c] for f in feats]) for c in feats[0]})], axis=1)


def _account(P: dict, tenant, acct, S: np.ndarray, R: np.ndarray) -> dict:
    """Every feature for one account's snapshots S (renewal dates R), as {name: array of len(S)}."""
    n = len(S)
    f = {"k_weeks": ((R - S) / DAY / 7.0).astype(float)}
    created, employees = P["account_info"][(tenant, acct)]
    f["age_days"] = np.clip(_days(S, np.datetime64(created)), 0, None)
    f["log_employees"] = np.full(n, np.log1p(float(employees)) if pd.notna(employees) else np.nan)

    # ---- product usage ----------------------------------------------------------------------------------------
    e = P["cols"]["product"].get((tenant, acct))
    if e is not None:
        t, (age, known) = e["t"], _ages(S, e)
        m28, mprev, m90, m8w = _window(age, known, 0, 28), _window(age, known, 28, 112), _window(age, known, 0, 90), _window(age, known, 0, 56)
        seen = (t[None, :] <= S[:, None]) & known
        f["has_product"] = seen.any(1).astype(float)
        f["p_28"] = m28.sum(1)
        f["p_trend"] = (m28.sum(1) / 4 + .25) / (mprev.sum(1) / 12 + .25)       # recent weekly rate vs the account's own baseline
        f["p_gap_days"] = np.minimum(np.where(seen, age, np.inf).min(1), CAP)
        wk = np.floor(age / 7.0)
        f["p_active_weeks_8"] = np.array([len(np.unique(wk[i][m8w[i]])) for i in range(n)], float)
        uid = e["uid"]
        f["p_users_90"] = np.array([len(set(uid[m90[i]]) - {""}) for i in range(n)], float)
        st = e["api"]
        api = m90 & ~np.isnan(st)[None, :]
        err = api & (np.nan_to_num(st, nan=0)[None, :] >= 400)
        f["p_err_rate_90"] = np.where(api.sum(1) > 0, err.sum(1) / np.maximum(api.sum(1), 1), 0.0)
        f.update(_buckets(age, known, prefix="p"))
        nm = e["name"]
        f["p_breadth_90"] = np.array([len(set(nm[m90[i]])) for i in range(n)], float)
    else:
        f.update(has_product=np.zeros(n), p_28=np.zeros(n), p_trend=np.ones(n),   # same value the formula gives for zero events
                 p_gap_days=np.full(n, CAP),
                 p_active_weeks_8=np.zeros(n), p_users_90=np.zeros(n), p_err_rate_90=np.zeros(n), p_breadth_90=np.zeros(n),
                 **_no_buckets(n, "p"))

    # ---- support tickets ---------------------------------------------------------------------------------------
    tk = P["cols"]["ticket"].get((tenant, acct))
    if tk is not None:
        t, (age, known) = tk["t"], _ages(S, tk)
        m30, m90, mprev = _window(age, known, 0, 30), _window(age, known, 0, 90), _window(age, known, 90, 180)
        m180 = _window(age, known, 0, 180)
        settled = m180 & (age >= 14)                                       # ticket attributes mature after a couple of weeks
        seen = (t[None, :] <= S[:, None]) & known
        f["has_tickets"] = seen.any(1).astype(float)
        f["tk_30"], f["tk_90"] = m30.sum(1), m90.sum(1)
        f.update(_buckets(age, known, prefix="tk"))
        f["tk_trend"] = (m90.sum(1) + .5) / (mprev.sum(1) + .5)
        f["tk_hi_share_90"] = np.where(m90.sum(1) > 0, (m90 & tk["hi"][None, :]).sum(1) / np.maximum(m90.sum(1), 1), 0.0)
        f["tk_cancel_180"] = (m180 & tk["cancel"][None, :]).sum(1)
        f["tk_outage_perf_90"] = (m90 & tk["outage"][None, :]).sum(1)
        f["tk_bad_csat_180"] = (settled & tk["bad_csat"][None, :]).sum(1)
        f["tk_reopen_180"] = (settled & tk["reopened"][None, :]).sum(1)
        fr = tk["reply"]
        f["tk_reply_min_med"] = np.array([np.nanmedian(fr[settled[i]]) if (settled[i] & ~np.isnan(fr)).any() else np.nan for i in range(n)])
    else:
        for c in ("has_tickets", "tk_30", "tk_90", "tk_hi_share_90", "tk_cancel_180", "tk_outage_perf_90", "tk_bad_csat_180", "tk_reopen_180"):
            f[c] = np.zeros(n)
        f["tk_trend"], f["tk_reply_min_med"] = np.ones(n), np.full(n, np.nan)
        f.update(_no_buckets(n, "tk"))

    # ---- email ------------------------------------------------------------------------------------------------
    em = P["cols"]["email"].get((tenant, acct))
    if em is not None:
        t, (age, known) = em["t"], _ages(S, em)
        inbound, outbound = em["inbound"], em["outbound"]
        m30, m90, mprev = _window(age, known, 0, 30), _window(age, known, 0, 90), _window(age, known, 90, 180)
        seen = (t[None, :] <= S[:, None]) & known
        f["has_email"] = seen.any(1).astype(float)
        f["em_in_30"], f["em_out_30"] = (m30 & inbound).sum(1), (m30 & outbound).sum(1)
        f.update(_buckets(age, known, extra=inbound, prefix="emi"))
        f.update(_buckets(age, known, extra=outbound, prefix="emo"))
        f["em_in_90"], f["em_out_90"] = (m90 & inbound).sum(1), (m90 & outbound).sum(1)
        f["em_reply_ratio_90"] = (f["em_in_90"] + .5) / (f["em_out_90"] + 1)
        f["em_in_trend"] = (f["em_in_90"] + .5) / ((mprev & inbound).sum(1) + .5)
        f["em_gap_in_days"] = np.minimum(np.where(seen & inbound[None, :], age, np.inf).min(1), CAP)
        f["em_bounce_90"] = (m90 & em["bounce"][None, :]).sum(1)
        f["em_auto_30"] = (m30 & em["auto"][None, :]).sum(1)
        champs = P["champion_emails"].get((tenant, acct), set())
        hit = np.array([a in champs for a in em["bounced"]], bool) & em["bounce"]
        f["champion_bounced_180"] = (_window(age, known, 0, 180) & hit[None, :]).any(1).astype(float)
    else:
        for c in ("has_email", "em_in_30", "em_out_30", "em_in_90", "em_out_90", "em_bounce_90", "em_auto_30", "champion_bounced_180"):
            f[c] = np.zeros(n)
        for pre in ("emi", "emo"):
            f.update(_no_buckets(n, pre))
        f["em_reply_ratio_90"], f["em_in_trend"], f["em_gap_in_days"] = np.full(n, .5), np.ones(n), np.full(n, CAP)   # what the formulas give for zero emails

    # ---- billing ----------------------------------------------------------------------------------------------
    b = P["cols"]["billing"].get((tenant, acct))
    if b is not None:
        t, (age, known) = b["t"], _ages(S, b)
        f["has_billing"] = ((t[None, :] <= S[:, None]) & known).any(1).astype(float)
        f["pay_fail_90"] = (_window(age, known, 0, 90) & b["pf"][None, :]).sum(1)
        f["pay_fail_365"] = (_window(age, known, 0, 365) & b["pf"][None, :]).sum(1)
        f["seat_drops_ever"] = (_window(age, known, 0, 5000) & b["drop"][None, :]).sum(1)
    else:
        f.update(has_billing=np.zeros(n), pay_fail_90=np.zeros(n), pay_fail_365=np.zeros(n), seat_drops_ever=np.zeros(n))
    return f
