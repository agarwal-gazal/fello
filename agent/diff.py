"""What changed since the previous Monday, per account. Pure functions over two stored score frames."""
from __future__ import annotations

import pandas as pd

RISK_JUMP = 0.10
COLS = ["tenant_id", "account_id", "kind", "from_band", "to_band", "from_risk", "to_risk", "detail"]


def alert_kinds(alerts) -> set:
    return {a["kind"] for a in (alerts or [])}


def compute(prev: pd.DataFrame | None, cur: pd.DataFrame, same_model: bool = True) -> pd.DataFrame:
    """prev=None means there is no earlier run: a first run is a baseline and has no changes (there is nothing to compare to).

    Risk jumps are suppressed when the model changed between the runs, because the two numbers are then not comparable."""
    if prev is None:
        return pd.DataFrame(columns=COLS)
    p = prev.set_index(["tenant_id", "account_id"])
    out = []
    for r in cur.itertuples():
        key = (r.tenant_id, r.account_id)
        old = p.loc[key] if key in p.index else None
        fb, fr = (old.band, float(old.risk)) if old is not None else (None, None)
        base = dict(tenant_id=r.tenant_id, account_id=r.account_id, from_band=fb, to_band=r.band, from_risk=fr, to_risk=float(r.risk))
        entered = r.band == "red" and fb != "red"
        if entered:
            out.append({**base, "kind": "entered_red", "detail": {"was_in_list": old is not None, "weeks_to_renewal": float(r.weeks_to_renewal)}})
        if old is not None and fb == "red" and r.band != "red":
            out.append({**base, "kind": "left_red", "detail": {"renewal_date": r.renewal_date}})
        new = alert_kinds(r.alerts) - (alert_kinds(old.alerts) if old is not None else set())
        if new:
            out.append({**base, "kind": "new_alert", "detail": {"kinds": sorted(new)}})
        if same_model and old is not None and not entered and float(r.risk) - fr >= RISK_JUMP:
            out.append({**base, "kind": "risk_jump", "detail": {"delta": round(float(r.risk) - fr, 4)}})
        if old is not None and bool(old.csm_active) and not bool(r.csm_active):
            out.append({**base, "kind": "csm_departed", "detail": {"csm_name": r.csm_name}})
    return pd.DataFrame(out, columns=COLS)
