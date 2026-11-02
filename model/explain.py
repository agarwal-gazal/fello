"""Turn a logistic score into reasons a CSM can read.

For a logistic model on standardised inputs, log-odds = intercept + sum_j coef_j * z_j exactly, so each feature's contribution is
coef_j * z_j (z = distance from the typical training account). Contributions are summed into a dozen CONCEPTS, because
'4-week bucket 3 of ticket volume' is not something a person acts on. Every reason carries the real numbers behind it.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .design import design


def _f(v, nd=0):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{nd}f}"


def _billing_text(r) -> str:
    """Say only what is true and relevant: name the billing facts that are non-zero, never '0 failed payments' as a risk reason."""
    parts = []
    if (r.get("pay_fail_90") or 0) > 0:
        parts.append(f"{_f(r.get('pay_fail_90'))} failed payment(s) in the last 90 days")
    elif (r.get("pay_fail_365") or 0) > 0:
        parts.append(f"{_f(r.get('pay_fail_365'))} failed payment(s) in the last year")
    if (r.get("seat_drops_ever") or 0) > 0:
        parts.append(f"{_f(r.get('seat_drops_ever'))} earlier seat reduction(s)")
    return "Billing history: " + " and ".join(parts) if parts else "Billing history differs from the typical account"


# (concept, regex over feature names, text built from the account's raw feature row)
CONCEPTS = [
    ("usage_trend", r"^p_(trend|recent_vs_base|mid_vs_base)$",
     lambda r: f"Product activity over the last 12 weeks is {_f(r.get('p_recent_vs_base', np.nan) * 100)}% of this account's own earlier level"),
    ("usage_level", r"^p_(b\d|28|users_90|breadth_90|err_rate_90)$",
     lambda r: f"Low product activity: {_f(r.get('p_28'))} events in the last 28 days, {_f(r.get('p_users_90'))} active users in 90 days"),
    ("usage_gap", r"^p_(gap_days|active_weeks_8)$|^has_product$",
     lambda r: f"Product silence: last activity {_f(r.get('p_gap_days'))} days ago, active in {_f(r.get('p_active_weeks_8'))} of the last 8 weeks"),
    ("ticket_volume", r"^tk_(b\d|30|90|trend|recent_vs_base|mid_vs_base)$",
     lambda r: f"Support load: {_f(r.get('tk_90'))} tickets in the last 90 days ({_f(r.get('tk_30'))} in the last 30)"),
    ("ticket_severity", r"^tk_(hi_share_90|outage_perf_90|bad_csat_180|reopen_180|reply_min_med)$",
     lambda r: f"Troubled tickets: {_f(r.get('tk_bad_csat_180'))} bad CSAT, {_f(r.get('tk_reopen_180'))} reopened, {_f(r.get('tk_outage_perf_90'))} outage/performance/bug in recent months"),
    ("cancellation_request", r"^tk_cancel_180$",
     lambda r: f"{_f(r.get('tk_cancel_180'))} support ticket(s) about cancelling in the last 6 months"),
    ("email_responsiveness", r"^(emi_|em_in|em_gap_in_days|em_reply_ratio_90|em_auto_30|has_email)",
     lambda r: f"Customer replies have faded: {_f(r.get('em_in_90'))} inbound emails in 90 days, last inbound {_f(r.get('em_gap_in_days'))} days ago"),
    ("outreach_volume", r"^(emo_|em_out)",
     lambda r: f"Outbound outreach: {_f(r.get('em_out_90'))} emails sent in the last 90 days"),
    ("champion_or_bounce", r"^(em_bounce_90|champion_bounced_180)$",
     lambda r: "Emails to a champion contact are bouncing: the champion may have left" if r.get("champion_bounced_180", 0) else f"{_f(r.get('em_bounce_90'))} emails bounced in the last 90 days"),
    ("billing", r"^(pay_fail_90|pay_fail_365|seat_drops_ever|has_billing)$",
     lambda r: _billing_text(r)),
    ("tenure_size", r"^(age_days|log_employees|seg_)",
     lambda r: f"Account profile: {_f(r.get('age_days') / 30.4)} months old"),
    ("timing", r"^k_weeks$", lambda r: "Time until renewal"),
]


# Wording for factors that LOWER risk. The risk templates describe a problem; reusing them here would say "low activity" for a good sign.
PROTECTIVE = {
    "usage_trend": "Product activity is holding up or growing versus this account's own earlier level",
    "usage_level": "Healthy product activity and a broad set of active users",
    "usage_gap": "Recently active in the product",
    "ticket_volume": "Light support load",
    "ticket_severity": "Few troubled tickets (low severity, good CSAT)",
    "cancellation_request": "No cancellation requests",
    "email_responsiveness": "The customer is replying to email",
    "outreach_volume": "Regular outreach from the CSM",
    "champion_or_bounce": "No bounced emails",
    "billing": "Clean billing history",
    "tenure_size": "Established account of a lower-risk size or tenure",
}


def concept_of(feature: str) -> str:
    for name, rx, _ in CONCEPTS:
        if re.search(rx, feature):
            return name
    return "other"


def contributions(model_dict: dict, feats: pd.DataFrame) -> pd.DataFrame:
    """One row per account, one column per feature: contribution to the log-odds relative to the typical training account."""
    pipe = model_dict["model"]
    X = design(feats, model_dict["columns"])[model_dict["columns"]]
    Z = pipe[:-1].transform(X)
    coef = pipe[-1].coef_[0]
    return pd.DataFrame(Z * coef, columns=model_dict["columns"], index=feats.index)


def explain(model_dict: dict, feats: pd.DataFrame, top: int = 3, min_effect: float = 0.08) -> pd.DataFrame:
    C = contributions(model_dict, feats)
    groups = {}
    for col in C.columns:
        groups.setdefault(concept_of(col), []).append(col)
    G = pd.DataFrame({g: C[cols].sum(axis=1) for g, cols in groups.items()})
    G = G.drop(columns=["timing"], errors="ignore")                 # a property of the calendar, not of the customer
    texts = {n: t for n, _, t in CONCEPTS}
    out = []
    raw = feats.reset_index(drop=True)
    for i in range(len(G)):
        row, g = raw.iloc[i], G.iloc[i]
        risk = [(c, float(v)) for c, v in g.sort_values(ascending=False).items() if v >= min_effect][:top]
        prot = [(c, float(v)) for c, v in g.sort_values().items() if v <= -min_effect][:1]
        mk = lambda c, v: {"concept": c, "effect": round(v, 3), "text": texts.get(c, lambda r: c)(row.to_dict())}
        mp = lambda c, v: {"concept": c, "effect": round(v, 3), "text": PROTECTIVE.get(c, "Better than typical")}
        out.append({"drivers": [mk(c, v) for c, v in risk], "protective": [mp(c, v) for c, v in prot],
                    "concept_effects": {c: round(float(v), 3) for c, v in g.items()}})
    return pd.DataFrame(out, index=feats.index)
