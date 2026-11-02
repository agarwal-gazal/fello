"""Deterministic drafts built ONLY from the stored score row (drivers, alerts, numbers). No model call, nothing invented.

Every number and date in a draft body is rendered from `facts`, and `facts` plus the driver/alert sentences are saved as the
draft's `reasons`, so a reviewer (and tests/test_agent.py) can check each claim against what the system stored.
An LLM rewrite stage could be plugged in after render(); it is off by default, and if added it must be re-checked against `reasons`.

Customer emails never mention scores, risk, or the words that imply them; they are a check-in written for the customer.
"""
from __future__ import annotations

import re

import pandas as pd

BANNED_IN_CUSTOMER_EMAIL = re.compile(r"risk|score|churn|probab|%|\bred\b|yellow|green|model|algorithm|likelihood", re.I)
DEDUPE_DAYS = 14
SUPPORT = {"ticket_volume", "ticket_severity", "cancellation_request"}
USAGE = {"usage_trend", "usage_level", "usage_gap"}


def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] if isinstance(name, str) and name.strip() else ""


def _date_text(d: str) -> str:
    t = pd.Timestamp(d)
    return f"{t.day} {t:%B %Y}"


def _weeks_text(w: float) -> str:
    n = int(round(w))
    return "about a week" if n <= 1 else f"about {n} weeks"


def facts_for(row, list_size: int, changes: list[str], asof: pd.Timestamp) -> dict:
    return {
        "account_name": row.account_name, "renewal_date": row.renewal_date, "renewal_date_text": _date_text(row.renewal_date),
        "weeks_text": _weeks_text(float(row.weeks_to_renewal)), "risk_pct": str(int(round(float(row.risk) * 100))),
        "band": row.band, "rank_in_csm": str(int(row.rank_in_csm)), "list_size": str(int(list_size)),
        "csm_name": row.csm_name, "asof": f"{asof:%Y-%m-%d}", "asof_text": _date_text(f"{asof:%Y-%m-%d}"),
        "renewal_source": row.renewal_source, "changes": changes}


CHANGE_TEXT = {"entered_red": "It moved into the red list this week.", "new_alert": "A new alert fired on it this week.",
               "risk_jump": "Its estimated risk rose sharply since last week.", "csm_departed": "Its CSM has left since last week.",
               "left_red": "It dropped out of the red list this week."}


def next_step(alerts: list[dict], drivers: list[dict]) -> str:
    kinds = {a["kind"] for a in alerts}
    if "cancel_request" in kinds:
        return "Call the customer this week to understand what is behind the cancellation question, before anything else."
    if "champion_left" in kinds:
        return "Find out whether the champion has left, and who now owns the relationship on their side."
    concepts = [d["concept"] for d in drivers]
    if any(c in SUPPORT for c in concepts):
        return "Review the recent support history and reach out to confirm the issues are resolved."
    if any(c in USAGE for c in concepts):
        return "Reach out to understand how the team is using the product and what is getting in the way."
    return "Review the account and reach out before the renewal conversation starts."


def csm_note(row, facts: dict, drivers: list[dict], alerts: list[dict], protective: list[dict], routing: bool, contact_note: str):
    f = facts
    lines = []
    if routing:
        lines += ["Hello,", "",
                  f"{f['account_name']} has no active owner: it is assigned to {f['csm_name']}, who has left. It renews on "
                  f"{f['renewal_date_text']}, {f['weeks_text']} from now, and the system rates it {row.band} "
                  f"(estimated {f['risk_pct']}% chance of churning or downgrading by a quarter or more). Please route it to a new CSM."]
        subject = f"Unowned at-risk account: {f['account_name']} renews {f['renewal_date_text']}"
    else:
        lines += [f"Hi {_first(f['csm_name'])},", "",
                  f"{f['account_name']} renews on {f['renewal_date_text']}, {f['weeks_text']} from now. It is number {f['rank_in_csm']} of "
                  f"{f['list_size']} on your list this week, with an estimated {f['risk_pct']}% chance of churning or downgrading by a "
                  f"quarter or more at renewal."]
        subject = f"Needs attention: {f['account_name']} renews {f['renewal_date_text']}"
    if f["changes"]:
        lines += ["", " ".join(CHANGE_TEXT[c] for c in f["changes"])]
    if alerts:
        lines += ["", "Alerts:"] + [f"- {a['text']}" for a in alerts]
    if drivers:
        lines += ["", "What the system sees:"] + [f"- {d['text']}" for d in drivers]
    # The 'protective' reasons are deliberately not shown: model/explain.py renders them with the risk-phrased template
    # ('Low product activity' for a factor that LOWERS risk), which would put a false sentence in front of a CSM.
    if row.renewal_source == "projected":
        lines += ["", "Note: this renewal date is projected from the last anniversary; no CRM renewal opportunity exists yet."]
    if row.data_note == "silent_for_90_days":
        lines += ["", "Note: there has been no activity in any source for 90 days. Silence can mean trouble or just a quiet customer."]
    elif row.data_note == "no_data_in_any_source":
        lines += ["", "Note: the system has no data for this account in any source, so this estimate rests on very little."]
    if contact_note:
        lines += ["", contact_note]
    lines += ["", f"Suggested next step: {next_step(alerts, drivers)}", "",
              f"Drafted automatically from the weekly run of {f['asof_text']}. Nothing has been sent."]
    return subject, "\n".join(lines)


def customer_email(row, facts: dict, drivers: list[dict], alerts: list[dict], contact_first: str):
    f = facts
    concepts = [d["concept"] for d in drivers]
    if any(a["kind"] == "cancel_request" for a in alerts):
        reason = "I saw your recent note about your plan, and I would like to understand what is behind it and see whether we can put it right."
    elif any(c in SUPPORT for c in concepts):
        reason = "I noticed you have raised a few questions with our support team recently, and I want to make sure they have been fully resolved."
    elif any(c in USAGE for c in concepts):
        reason = "I would love to hear how your team is using the product today and whether there is anything we can do to help you get more from it."
    else:
        reason = "I would like to hear how things are going and whether there is anything we can do to help your team."
    greet = f"Hi {contact_first}," if contact_first else "Hello,"
    body = "\n".join([greet, "",
                      f"I am {_first(f['csm_name'])}, your customer success manager. Your renewal is coming up on {f['renewal_date_text']}, "
                      f"and I would like to find a time to talk before then.", "", reason, "",
                      "Would a short call in the next week or so work for you? Tell me a time that suits you and I will set it up.", "",
                      f"Best regards,\n{f['csm_name']}"])
    return f"Checking in ahead of your renewal on {f['renewal_date_text']}", body


def pick_contact(contacts: pd.DataFrame | None, bounced: set) -> tuple[str | None, str]:
    """Champion first, then decision maker, then anyone; never an address that has bounced."""
    if contacts is None or contacts.empty:
        return None, ""
    c = contacts[~contacts.email.fillna("").str.lower().isin(bounced) & contacts.email.notna()].copy()
    if c.empty:
        return None, ""
    c["rank"] = c.is_champion.eq("t").map({True: 0, False: 2}) + c.is_decision_maker.eq("t").map({True: 0, False: 1})
    top = c.sort_values(["rank", "email"]).iloc[0]
    return top.email, _first(top.first_name)


def generate(stored: pd.DataFrame, changes: pd.DataFrame, contacts: dict, bounced: dict, existing: set, asof: pd.Timestamp) -> pd.DataFrame:
    """One row per draft to insert. `existing` = {(tenant, account, kind)} already pending/approved/sent within DEDUPE_DAYS."""
    chg = changes.groupby(["tenant_id", "account_id"]).kind.apply(list).to_dict() if len(changes) else {}
    sizes = stored.groupby(["tenant_id", "owner_id"]).size().to_dict()
    created = f"{asof:%Y-%m-%d} 06:00:00+00"
    out = []
    for r in stored.itertuples():
        key = (r.tenant_id, r.account_id)
        departed = not r.csm_active
        wants_note = (departed and r.band in ("red", "yellow")) or (not departed and (r.band == "red" or (r.band == "yellow" and r.alerts)))
        wants_mail = (not departed) and r.band == "red"
        if not (wants_note or wants_mail):
            continue
        facts = facts_for(r, sizes[(r.tenant_id, r.owner_id)], [c for c in chg.get(key, []) if c in CHANGE_TEXT], asof)
        to, first = pick_contact(contacts.get(key), bounced.get(key, set()))
        base_reasons = {"facts": facts, "drivers": [d["text"] for d in r.drivers], "alerts": [a["text"] for a in r.alerts],
                        "score_run": r.run_id, "data_note": r.data_note}
        if wants_note and (key[0], key[1], "csm_note") not in existing:
            note = "" if (departed or to) else "No working contact address is on file for this customer; a customer email could not be drafted."
            subj, body = csm_note(r, facts, r.drivers, r.alerts, r.protective, departed, note)
            out.append(dict(tenant_id=r.tenant_id, account_id=r.account_id, owner_id=r.owner_id, kind="csm_note", to_address=None,
                            subject=subj, body=body, reasons=base_reasons, created_at=created))
        if wants_mail and to and (key[0], key[1], "customer_email") not in existing:
            subj, body = customer_email(r, facts, r.drivers, r.alerts, first)
            out.append(dict(tenant_id=r.tenant_id, account_id=r.account_id, owner_id=r.owner_id, kind="customer_email", to_address=to,
                            subject=subj, body=body, reasons={**base_reasons, "contact": {"to": to, "first_name": first}}, created_at=created))
    return pd.DataFrame(out)
