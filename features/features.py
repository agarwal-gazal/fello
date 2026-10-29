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


def _acct_arrays(df: pd.DataFrame, ts: str) -> dict:
    out = {}
    if df is None or df.empty:
        return out
    for key, g in df.sort_values(ts).groupby(["tenant_id", "account_id"], sort=False):
        out[key] = g
    return out


def prepare(tables: dict) -> dict:
    """Index the fact tables by (tenant, account) once; datetimes as tz-naive UTC numpy."""
    def norm(df, cols):
        df = df.copy()
        for c in cols:
            df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").dt.tz_convert(None)
        return df
    p = {
        "product": _acct_arrays(norm(tables["product_event"], ["event_ts", "known_at"]), "event_ts"),
        "ticket": _acct_arrays(norm(tables["ticket"], ["created_at", "known_at", "solved_at"]), "created_at"),
        "email": _acct_arrays(norm(tables["email"], ["sent_at", "known_at"]), "sent_at"),
        "billing": _acct_arrays(norm(tables["billing_event"], ["event_at", "known_at"]), "event_at"),
        "invoice": _acct_arrays(norm(tables["invoice"], ["created_at", "known_at"]), "created_at"),
    }
    acc = tables["account"].copy()
    acc["created_at"] = pd.to_datetime(acc["created_at"], utc=True, format="ISO8601").dt.tz_convert(None)
    p["account"] = acc.set_index(["tenant_id", "account_id"])
    ct = tables["contact"]
    champs = ct[ct.is_champion.astype(str).str.lower().isin(["t", "true", "1"])]
    p["champion_emails"] = champs.groupby(["tenant_id", "account_id"]).email.apply(lambda s: set(s.dropna().str.lower())).to_dict()
    return p


def _window(S, t, known, lo, hi):
    """Mask (n_snap, n_event): event in (S-hi, S-lo] days AND already known at S."""
    age = _days(S[:, None], t[None, :])
    return (age > lo) & (age <= hi) & (known[None, :] <= S[:, None])


NB = 9                                  # 4-week buckets going back 36 weeks


def _buckets(S, t, kn, extra=None, prefix="x"):
    """Event counts in consecutive 28-day buckets before the snapshot (b0 = most recent), plus the account's own long-run
    baseline (buckets 4-8) and recent-vs-baseline ratio. Gives the model the shape of the trajectory, not one summary number."""
    cols = {}
    for j in range(NB):
        m = _window(S, t, kn, 28 * j, 28 * (j + 1))
        if extra is not None:
            m = m & extra[None, :]
        cols[f"{prefix}_b{j}"] = m.sum(1).astype(float)
    recent = (cols[f"{prefix}_b0"] + cols[f"{prefix}_b1"] + cols[f"{prefix}_b2"]) / 3
    base = sum(cols[f"{prefix}_b{j}"] for j in range(3, NB)) / (NB - 3)
    cols[f"{prefix}_recent_vs_base"] = (recent + .5) / (base + .5)
    cols[f"{prefix}_mid_vs_base"] = ((cols[f"{prefix}_b2"] + cols[f"{prefix}_b3"] + cols[f"{prefix}_b4"]) / 3 + .5) / (base + .5)
    return cols


def compute(snaps: pd.DataFrame, tables_or_prepared: dict) -> pd.DataFrame:
    P = tables_or_prepared if "champion_emails" in tables_or_prepared else prepare(tables_or_prepared)
    snaps = snaps.copy()
    snaps["snap_at"] = pd.to_datetime(snaps["snap_at"], utc=True).dt.tz_convert(None)
    snaps["renewal_date"] = pd.to_datetime(snaps["renewal_date"])
    rows = []
    for (tenant, acct), g in snaps.groupby(["tenant_id", "account_id"], sort=False):
        S = g["snap_at"].values.astype("datetime64[ns]")
        n = len(S)
        f = {"k_weeks": ((g["renewal_date"].values.astype("datetime64[ns]") - S) / DAY / 7.0).astype(float)}
        a = P["account"].loc[(tenant, acct)]
        f["age_days"] = np.clip(_days(S, np.datetime64(a.created_at)), 0, None)
        f["log_employees"] = np.full(n, np.log1p(float(a.employees)) if pd.notna(a.employees) else np.nan)

        # ---- product usage ----------------------------------------------------------------------------------------
        e = P["product"].get((tenant, acct))
        if e is not None:
            t, kn = e.event_ts.values.astype("datetime64[ns]"), e.known_at.values.astype("datetime64[ns]")
            m28, mprev, m90, m8w = _window(S, t, kn, 0, 28), _window(S, t, kn, 28, 112), _window(S, t, kn, 0, 90), _window(S, t, kn, 0, 56)
            seen = (t[None, :] <= S[:, None]) & (kn[None, :] <= S[:, None])
            f["has_product"] = seen.any(1).astype(float)
            f["p_28"] = m28.sum(1)
            f["p_trend"] = (m28.sum(1) / 4 + .25) / (mprev.sum(1) / 12 + .25)       # recent weekly rate vs the account's own baseline
            age = np.where(seen, _days(S[:, None], t[None, :]), np.inf)
            f["p_gap_days"] = np.minimum(age.min(1), CAP)
            wk = np.floor(_days(S[:, None], t[None, :]) / 7.0)
            f["p_active_weeks_8"] = np.array([len(np.unique(wk[i][m8w[i]])) for i in range(n)], float)
            uid = e.user_id.fillna("").values
            f["p_users_90"] = np.array([len(set(uid[m90[i]]) - {""}) for i in range(n)], float)
            st = pd.to_numeric(e.api_status, errors="coerce").values
            api = m90 & ~np.isnan(st)[None, :]
            err = api & (np.nan_to_num(st, nan=0)[None, :] >= 400)
            f["p_err_rate_90"] = np.where(api.sum(1) > 0, err.sum(1) / np.maximum(api.sum(1), 1), 0.0)
            f.update(_buckets(S, t, kn, prefix="p"))
            nm = e.event_name.values
            f["p_breadth_90"] = np.array([len(set(nm[m90[i]])) for i in range(n)], float)
        else:
            f.update(has_product=np.zeros(n), p_28=np.zeros(n), p_trend=np.ones(n),   # same value the formula gives for zero events
                      p_gap_days=np.full(n, CAP),
                     p_active_weeks_8=np.zeros(n), p_users_90=np.zeros(n), p_err_rate_90=np.zeros(n), p_breadth_90=np.zeros(n),
                     **{k: (np.ones(n) if "vs_base" in k else np.zeros(n)) for k in _buckets(S, np.array([], "datetime64[ns]"), np.array([], "datetime64[ns]"), prefix="p")})

        # ---- support tickets ---------------------------------------------------------------------------------------
        tk = P["ticket"].get((tenant, acct))
        if tk is not None:
            t, kn = tk.created_at.values.astype("datetime64[ns]"), tk.known_at.values.astype("datetime64[ns]")
            m30, m90, mprev = _window(S, t, kn, 0, 30), _window(S, t, kn, 0, 90), _window(S, t, kn, 90, 180)
            m180 = _window(S, t, kn, 0, 180)
            settled = m180 & (_days(S[:, None], t[None, :]) >= 14)          # ticket attributes mature after a couple of weeks
            seen = (t[None, :] <= S[:, None]) & (kn[None, :] <= S[:, None])
            f["has_tickets"] = seen.any(1).astype(float)
            f["tk_30"], f["tk_90"] = m30.sum(1), m90.sum(1)
            f.update(_buckets(S, t, kn, prefix="tk"))
            f["tk_trend"] = (m90.sum(1) + .5) / (mprev.sum(1) + .5)
            hi = tk.priority.isin(["high", "urgent"]).values
            f["tk_hi_share_90"] = np.where(m90.sum(1) > 0, (m90 & hi[None, :]).sum(1) / np.maximum(m90.sum(1), 1), 0.0)
            f["tk_cancel_180"] = (m180 & (tk.category.values == "cancellation")[None, :]).sum(1)
            f["tk_outage_perf_90"] = (m90 & tk.category.isin(["outage", "performance", "bug"]).values[None, :]).sum(1)
            f["tk_bad_csat_180"] = (settled & (tk.csat.values == "bad")[None, :]).sum(1)
            f["tk_reopen_180"] = (settled & tk.is_reopened.astype(str).str.lower().isin(["t", "true"]).values[None, :]).sum(1)
            fr = pd.to_numeric(tk.first_reply_minutes, errors="coerce").values
            f["tk_reply_min_med"] = np.array([np.nanmedian(fr[settled[i]]) if (settled[i] & ~np.isnan(fr)).any() else np.nan for i in range(n)])
        else:
            for c in ("has_tickets", "tk_30", "tk_90", "tk_hi_share_90", "tk_cancel_180", "tk_outage_perf_90", "tk_bad_csat_180", "tk_reopen_180"):
                f[c] = np.zeros(n)
            f["tk_trend"], f["tk_reply_min_med"] = np.ones(n), np.full(n, np.nan)
            f.update({k: (np.ones(n) if "vs_base" in k else np.zeros(n)) for k in _buckets(S, np.array([], "datetime64[ns]"), np.array([], "datetime64[ns]"), prefix="tk")})

        # ---- email ------------------------------------------------------------------------------------------------
        em = P["email"].get((tenant, acct))
        if em is not None:
            t, kn = em.sent_at.values.astype("datetime64[ns]"), em.known_at.values.astype("datetime64[ns]")
            inbound = ((em.direction.values == "inbound") & ~em.is_auto_reply.astype(str).str.lower().isin(["t", "true"]).values
                       & ~em.is_bounce.astype(str).str.lower().isin(["t", "true"]).values)
            outbound = em.direction.values == "outbound"
            m30, m90, mprev = _window(S, t, kn, 0, 30), _window(S, t, kn, 0, 90), _window(S, t, kn, 90, 180)
            seen = (t[None, :] <= S[:, None]) & (kn[None, :] <= S[:, None])
            f["has_email"] = seen.any(1).astype(float)
            f["em_in_30"], f["em_out_30"] = (m30 & inbound).sum(1), (m30 & outbound).sum(1)
            f.update(_buckets(S, t, kn, extra=inbound, prefix="emi"))
            f.update(_buckets(S, t, kn, extra=outbound, prefix="emo"))
            f["em_in_90"], f["em_out_90"] = (m90 & inbound).sum(1), (m90 & outbound).sum(1)
            f["em_reply_ratio_90"] = (f["em_in_90"] + .5) / (f["em_out_90"] + 1)
            f["em_in_trend"] = (f["em_in_90"] + .5) / ((mprev & inbound).sum(1) + .5)
            age = np.where(seen & inbound[None, :], _days(S[:, None], t[None, :]), np.inf)
            f["em_gap_in_days"] = np.minimum(age.min(1), CAP)
            f["em_bounce_90"] = (m90 & em.is_bounce.astype(str).str.lower().isin(["t", "true"]).values[None, :]).sum(1)
            f["em_auto_30"] = (m30 & em.is_auto_reply.astype(str).str.lower().isin(["t", "true"]).values[None, :]).sum(1)
            champs = P["champion_emails"].get((tenant, acct), set())
            hit = em.bounced_address.fillna("").str.lower().isin(champs).values & em.is_bounce.astype(str).str.lower().isin(["t", "true"]).values
            f["champion_bounced_180"] = (_window(S, t, kn, 0, 180) & hit[None, :]).any(1).astype(float)
        else:
            for c in ("has_email", "em_in_30", "em_out_30", "em_in_90", "em_out_90", "em_bounce_90", "em_auto_30", "champion_bounced_180"):
                f[c] = np.zeros(n)
            for pre in ("emi", "emo"):
                f.update({k: (np.ones(n) if "vs_base" in k else np.zeros(n)) for k in _buckets(S, np.array([], "datetime64[ns]"), np.array([], "datetime64[ns]"), prefix=pre)})
            f["em_reply_ratio_90"], f["em_in_trend"], f["em_gap_in_days"] = np.full(n, .5), np.ones(n), np.full(n, CAP)   # what the formulas give for zero emails

        # ---- billing ----------------------------------------------------------------------------------------------
        b = P["billing"].get((tenant, acct))
        if b is not None:
            t, kn = b.event_at.values.astype("datetime64[ns]"), b.known_at.values.astype("datetime64[ns]")
            pf = (b.event_type.values == "invoice.payment_failed")
            drop = ((b.event_type.values == "customer.subscription.updated") & (pd.to_numeric(b.quantity, errors="coerce").values
                    < pd.to_numeric(b.previous_quantity, errors="coerce").values))
            f["has_billing"] = ((t[None, :] <= S[:, None]) & (kn[None, :] <= S[:, None])).any(1).astype(float)
            f["pay_fail_90"] = (_window(S, t, kn, 0, 90) & pf[None, :]).sum(1)
            f["pay_fail_365"] = (_window(S, t, kn, 0, 365) & pf[None, :]).sum(1)
            f["seat_drops_ever"] = (_window(S, t, kn, 0, 5000) & drop[None, :]).sum(1)
        else:
            f.update(has_billing=np.zeros(n), pay_fail_90=np.zeros(n), pay_fail_365=np.zeros(n), seat_drops_ever=np.zeros(n))

        out = g[["tenant_id", "account_id", "snap_at", "renewal_date"] + [c for c in g.columns if c not in
                ("tenant_id", "account_id", "snap_at", "renewal_date")]].reset_index(drop=True)
        rows.append(pd.concat([out, pd.DataFrame(f)], axis=1))
    return pd.concat(rows, ignore_index=True)
