"""Event alerts: things a CSM must see regardless of the statistical score.

The model ranks accounts by how much their behaviour looks like past churners. A customer writing 'considering cancelling' is not
a pattern to infer, it is a statement, and it has its own measured outcome. Each alert sets a FLOOR on the account's priority; the
calibrated model risk is never altered, so both numbers stay honest and visible.

Floors come from the backtest tables, not intuition (8 weeks out, all renewals up to Oct 2026):
    cancellation ticket in the last 180d: 154 renewals, 41.6% churned or downgraded vs 18.2% base   -> floor 0.40 (red)
    champion address bounced:               5 renewals, 40% (tiny sample, so only a yellow floor and labelled as such)
"""
from __future__ import annotations

import pandas as pd

CANCEL_WINDOW_DAYS = 45
CHAMPION_WINDOW_DAYS = 60
YELLOW_FLOOR = 0.18


def alerts_for(prepared: dict, tenant: str, account: str, asof: pd.Timestamp) -> list[dict]:
    out = []
    asof = pd.Timestamp(asof).tz_localize(None) if pd.Timestamp(asof).tzinfo else pd.Timestamp(asof)
    tk = prepared["ticket"].get((tenant, account))
    if tk is not None:
        seen = tk[(tk.known_at <= asof) & (tk.created_at <= asof) & (tk.created_at > asof - pd.Timedelta(days=CANCEL_WINDOW_DAYS))
                  & (tk.category == "cancellation")]
        if len(seen):
            last = seen.sort_values("created_at").iloc[-1]
            out.append({"kind": "cancel_request", "floor": 0.40, "when": str(last.created_at.date()),
                        "text": f"Customer asked about cancelling on {last.created_at.date()} (support ticket)",
                        "evidence": "41.6% of renewals with a cancellation ticket churned or downgraded (n=154) vs 18.2% base"})
    em = prepared["email"].get((tenant, account))
    champs = prepared["champion_emails"].get((tenant, account), set())
    if em is not None and champs:
        b = em[(em.known_at <= asof) & (em.sent_at <= asof) & (em.sent_at > asof - pd.Timedelta(days=CHAMPION_WINDOW_DAYS))
               & em.is_bounce.astype(str).str.lower().isin(["t", "true"]) & em.bounced_address.fillna("").str.lower().isin(champs)]
        if len(b):
            out.append({"kind": "champion_left", "floor": YELLOW_FLOOR, "when": str(b.sent_at.max().date()),
                        "text": f"Email to the champion contact bounced on {b.sent_at.max().date()}: they may have left",
                        "evidence": "only 5 historical cases (40% churned): treat as a prompt to check, not a verdict"})
    return out
