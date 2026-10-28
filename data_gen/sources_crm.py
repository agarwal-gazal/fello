"""Salesforce (CRM) and Stripe (billing) emitters.

Salesforce is a *current-state snapshot* (no history) - the classic reason a naive
'as of date X' feature can leak the future. Stripe is a snapshot plus an event stream
(webhooks) which can be duplicated, late, and out of order.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

import numpy as np

from .config import EXPORT_AT, EXPORT_DATE, MESS, N_PRE, WINDOW_START, TenantConfig
from .mess import UTC, at, iso_z, sync_time
from .names import messy_variant, PERSONAL
from .world import Account, rid, week_idx

SEG_LABEL = {"smb": "SMB", "mid": "Mid-Market", "ent": "Enterprise"}


def add_months(d: date, m: int) -> date:
    y, mo = divmod(d.month - 1 + m, 12)
    y, mo = d.year + y, mo + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def clamp_sync(rng, avail: datetime) -> datetime:
    """Snapshot tables are always fully loaded by the cutoff."""
    return min(sync_time(rng, avail, p_late=.02, late_days=(1, 6)), EXPORT_AT - timedelta(hours=1))


def _site(rng, domain: str) -> str | None:
    r = rng.random()
    if r < MESS["website_missing"]:
        return None
    forms = (f"https://www.{domain}", domain, f"http://{domain}/", f"www.{domain}",
             f"https://{domain}", f"WWW.{domain.upper()}")
    return forms[int(rng.integers(0, len(forms)))]


# =============================================================================
# Salesforce
# =============================================================================
def emit_salesforce(t: TenantConfig, accs: list[Account], csms: list[dict], rng, links: list):
    out = {"accounts": [], "contacts": [], "opportunities": [], "users": []}
    for c in csms:
        out["users"].append({
            "attributes": {"type": "User"}, "Id": c["user_id"], "Name": c["name"], "Email": c["email"],
            "Title": "Customer Success Manager", "IsActive": c["active"],
            "_synced_at": iso_z(EXPORT_AT - timedelta(hours=2))})

    for a in accs:
        a.sf_id = rid(rng, "001", 15)
        created = at(a.contract_start - timedelta(days=int(rng.integers(5, 40))), 14)
        if a.churned:
            last_mod = at(a.end_date + timedelta(days=int(rng.integers(0, 10))), 15)
        else:
            last_mod = EXPORT_AT - timedelta(days=float(min(rng.exponential(60), 600)))
        last_mod = min(max(last_mod, created + timedelta(days=1)), EXPORT_AT - timedelta(hours=3))

        typ = "Customer"
        if a.churned and rng.random() < .9:          # 10% of churned accounts are never updated
            typ = "Former Customer"

        nxt = next((r.day for r in a.renewals if r.day > EXPORT_DATE), None)
        end_field = nxt or (a.end_date or a.contract_start + timedelta(days=365))
        if rng.random() < MESS["sf_stale_end"]:      # nobody updated it after the last renewal
            prev = [r.day for r in a.renewals if r.day <= EXPORT_DATE]
            end_field = prev[-1] if prev else a.contract_start + timedelta(days=365)
        seats_now = a.seats_at(min(EXPORT_DATE, a.end_date or EXPORT_DATE))
        arr = seats_now * a.unit_price * 12
        if rng.random() < MESS["sf_stale_arr"]:
            arr = a.seats0 * a.unit_price * 12
        website = _site(rng, a.domain if rng.random() > .03 or not a.alias_domain else a.alias_domain)
        if "workspace_name_mismatch" in a.planted_mess:
            website = None
        out["accounts"].append({
            "attributes": {"type": "Account"}, "Id": a.sf_id, "Name": a.legal, "Website": website,
            "Industry": a.industry, "NumberOfEmployees": a.employees, "Segment__c": SEG_LABEL[a.segment],
            "OwnerId": a.csm_id, "Type": typ, "Contract_End_Date__c": end_field.isoformat(),
            "ARR__c": round(float(arr), 2), "CreatedDate": iso_z(created),
            "LastModifiedDate": iso_z(last_mod), "_synced_at": iso_z(clamp_sync(rng, last_mod))})
        links.append((t.tenant_id, "salesforce_account", a.sf_id, a.true_id))

        # duplicate account record (web form / manual entry)
        dup_id = None
        if rng.random() < MESS["sf_dup"] and not a.role:
            dup_id = a.sf_dup_id = rid(rng, "001", 15)
            dcreated = min(at(a.contract_start + timedelta(days=int(rng.integers(60, 400))), 11),
                           EXPORT_AT - timedelta(days=2))
            out["accounts"].append({
                "attributes": {"type": "Account"}, "Id": dup_id,
                "Name": messy_variant(rng, a.base_name, a.legal),
                "Website": None if rng.random() < .6 else _site(rng, a.domain), "Industry": None,
                "NumberOfEmployees": None, "Segment__c": None, "OwnerId": a.csm_id, "Type": "Prospect",
                "Contract_End_Date__c": None, "ARR__c": None, "CreatedDate": iso_z(dcreated),
                "LastModifiedDate": iso_z(dcreated), "_synced_at": iso_z(clamp_sync(rng, dcreated))})
            links.append((t.tenant_id, "salesforce_account", dup_id, a.true_id))

        # contacts
        for c in a.contacts:
            on_dup = bool(dup_id) and rng.random() < .35
            ccreated = min(at(a.contract_start + timedelta(days=int(rng.integers(0, 60))), 12),
                           EXPORT_AT - timedelta(days=1))
            out["contacts"].append({
                "attributes": {"type": "Contact"}, "Id": c.contact_id,
                "AccountId": dup_id if on_dup else a.sf_id,
                "FirstName": c.first, "LastName": c.last,
                "Email": None if rng.random() < .04 else c.email, "Title": c.title,
                "Is_Champion__c": c.sf_champion_flag, "Is_Decision_Maker__c": c.role == "decision_maker",
                "CreatedDate": iso_z(ccreated), "LastModifiedDate": iso_z(ccreated),
                "_synced_at": iso_z(clamp_sync(rng, ccreated))})

        # opportunities (final state only - history is gone)
        for r in a.renewals:
            ocreated = at(r.day - timedelta(days=120), 10)
            if ocreated > EXPORT_AT - timedelta(days=1) or ocreated < at(a.contract_start, 10):
                continue
            if r.outcome == "pending":
                stage = "Prospecting" if (r.day - EXPORT_DATE).days > 60 else "Negotiation/Review"
                closed = won = False
                amount = r.seats_before * a.unit_price * 12
                lm = min(ocreated + timedelta(days=float(rng.integers(1, 90))), EXPORT_AT - timedelta(hours=5))
            else:
                closed = True
                if r.outcome == "churned":
                    won, amount = False, r.seats_before * a.unit_price * 12
                    stage = "Closed Lost"
                else:
                    won, amount = True, r.seats_after * a.unit_price * 12
                    stage = "Closed Won"
                if rng.random() < (.10 if not won else .05):          # stale: never closed in CRM
                    stage, closed, won = "Negotiation/Review", False, False
                lm = min(at(r.day + timedelta(days=int(rng.integers(0, 6))), 14), EXPORT_AT - timedelta(hours=5))
            out["opportunities"].append({
                "attributes": {"type": "Opportunity"}, "Id": rid(rng, "006", 15),
                "AccountId": dup_id if (dup_id and rng.random() < .5) else a.sf_id,
                "Type": "Renewal", "StageName": stage, "CloseDate": r.day.isoformat(),
                "Amount": round(float(amount), 2), "IsClosed": closed, "IsWon": won,
                "CreatedDate": iso_z(ocreated), "LastModifiedDate": iso_z(lm),
                "_synced_at": iso_z(clamp_sync(rng, lm))})
        for ev in a.seat_events:
            if ev.kind in ("upgrade", "renewal_expand") and ev.day >= WINDOW_START:
                prev = a.seats_at(ev.day - timedelta(days=1))
                delta = max(ev.seats - prev, 0) * a.unit_price * 12
                oc = at(ev.day - timedelta(days=30), 10)
                lm = min(at(ev.day, 15), EXPORT_AT - timedelta(hours=5))
                out["opportunities"].append({
                    "attributes": {"type": "Opportunity"}, "Id": rid(rng, "006", 15), "AccountId": a.sf_id,
                    "Type": "Upsell", "StageName": "Closed Won", "CloseDate": ev.day.isoformat(),
                    "Amount": round(float(delta), 2), "IsClosed": True, "IsWon": True,
                    "CreatedDate": iso_z(oc), "LastModifiedDate": iso_z(lm),
                    "_synced_at": iso_z(clamp_sync(rng, lm))})
    return out


# =============================================================================
# Stripe
# =============================================================================
def _billing_dates(a: Account, upto: date) -> list[date]:
    """Invoice creation dates inside the retained history window."""
    dates, k = [], 0
    while True:
        d = add_months(a.contract_start, k * (12 if a.billing == "annual" else 1))
        if d > upto:
            break
        if d >= WINDOW_START:
            dates.append(d)
        k += 1
    return dates


def _period(a: Account, as_of: date) -> tuple[date, date]:
    step = 12 if a.billing == "annual" else 1
    k = 0
    while add_months(a.contract_start, (k + 1) * step) <= as_of:
        k += 1
    return add_months(a.contract_start, k * step), add_months(a.contract_start, (k + 1) * step)


def emit_stripe(t: TenantConfig, accs: list[Account], rng, links: list):
    out = {"customers": [], "subscriptions": [], "invoices": [], "events": []}
    events: list[dict] = []

    def add_event(etype: str, day_or_dt, obj: dict, prev: dict | None = None, hour: float = 11.0):
        dt = day_or_dt if isinstance(day_or_dt, datetime) else at(day_or_dt, hour + float(rng.uniform(-1, 4)))
        if dt > EXPORT_AT:
            return
        ev = {"id": rid(rng, "evt_", 24), "object": "event", "type": etype, "created": int(dt.timestamp()),
              "livemode": True, "data": {"object": obj, **({"previous_attributes": prev} if prev else {})}}
        sync = sync_time(rng, dt, p_late=MESS["stripe_late"], late_days=(1, 12))
        if sync <= EXPORT_AT:
            events.append({**ev, "_synced_at": iso_z(sync)})
        if rng.random() < MESS["stripe_dup_webhook"]:          # webhook retry
            s2 = sync + timedelta(minutes=float(rng.uniform(5, 600)))
            if s2 <= EXPORT_AT:
                events.append({**ev, "_synced_at": iso_z(s2)})

    for a in accs:
        a.stripe_cus = rid(rng, "cus_", 14)
        a.stripe_sub = rid(rng, "sub_", 14)
        created = at(a.contract_start, 10)
        # who gets the invoice?
        r = rng.random()
        champion, dm = a.contacts[0], next((c for c in a.contacts if c.role == "decision_maker"), a.contacts[0])
        if r < .55:
            email = f"{str(rng.choice(['billing', 'finance', 'accounts', 'ap']))}@{a.domain}"
        elif r < .80:
            email = dm.email
        elif r < .90:
            email = champion.email
        else:
            email = f"{a.base_name.split()[0].lower()}{int(rng.integers(1, 99))}@{str(rng.choice(PERSONAL))}"
        meta = {}
        if rng.random() < t.stripe_sf_link and "no_stripe_sf_link" not in a.planted_mess:
            wrong = a.sf_dup_id and rng.random() < .15
            meta["salesforce_id"] = a.sf_dup_id if wrong else a.sf_id
        out["customers"].append({
            "id": a.stripe_cus, "object": "customer", "created": int(created.timestamp()),
            "name": messy_variant(rng, a.base_name, a.legal), "email": email, "livemode": True,
            "metadata": meta, "delinquent": False, "_synced_at": iso_z(EXPORT_AT - timedelta(hours=3))})
        links.append((t.tenant_id, "stripe_customer", a.stripe_cus, a.true_id))
        if a.contract_start >= WINDOW_START:
            add_event("customer.created", a.contract_start, {"id": a.stripe_cus, "object": "customer",
                                                              "email": email}, hour=10)

        last_day = min(EXPORT_DATE - timedelta(days=1), (a.end_date or EXPORT_DATE) - timedelta(days=1))

        # ---- invoices + failed-payment events ------------------------------
        open_invoice = False
        for d in _billing_dates(a, last_day):
            seats = a.seats_at(d)
            amount = int(round(seats * a.unit_price * (12 if a.billing == "annual" else 1) * 100))
            wk = week_idx(d)
            h_pay = float(a.h[min(max(N_PRE + wk - 3, 0), len(a.h) - 1)])
            p_fail = .015 + .20 * (1 - h_pay) ** 2 + (.01 if a.segment == "smb" else 0)
            created_dt = at(d, 6)
            attempts, paid_dt, status = 1, created_dt + timedelta(hours=1), "paid"
            if rng.random() < p_fail:
                offsets = [0, 3, 5, 7]
                n_fail = 1
                while n_fail < 4 and rng.random() > .55:
                    n_fail += 1
                for j in range(n_fail):
                    add_event("invoice.payment_failed", created_dt + timedelta(days=offsets[j], hours=2),
                              {"object": "invoice", "customer": a.stripe_cus, "amount_due": amount,
                               "attempt_count": j + 1})
                if n_fail == 4:
                    status, attempts, paid_dt = "uncollectible", 4, None
                else:
                    attempts, paid_dt = n_fail + 1, created_dt + timedelta(days=offsets[n_fail], hours=2)
                if paid_dt and paid_dt > EXPORT_AT:
                    status, paid_dt, attempts = "open", None, n_fail
                    open_invoice = True
            billing_reason = "subscription_create" if d == a.contract_start else "subscription_cycle"
            out["invoices"].append({
                "id": rid(rng, "in_", 24), "object": "invoice", "customer": a.stripe_cus,
                "subscription": a.stripe_sub, "amount_due": amount,
                "amount_paid": amount if status == "paid" else 0, "currency": "usd", "status": status,
                "attempt_count": attempts, "billing_reason": billing_reason,
                "created": int(created_dt.timestamp()),
                "status_transitions": {"paid_at": int(paid_dt.timestamp()) if paid_dt else None},
                "_synced_at": iso_z(min(sync_time(rng, paid_dt or created_dt, p_late=.02, late_days=(1, 8)),
                                        EXPORT_AT))})

        # ---- subscription events -------------------------------------------
        if a.contract_start >= WINDOW_START:
            add_event("customer.subscription.created", a.contract_start,
                      {"id": a.stripe_sub, "object": "subscription", "customer": a.stripe_cus,
                       "quantity": a.seats0, "status": "active"}, hour=10)
        prev_q = a.seats0
        for ev in a.seat_events:
            if ev.kind == "start":
                continue
            if ev.day > EXPORT_DATE:
                break
            add_event("customer.subscription.updated", ev.day,
                      {"id": a.stripe_sub, "object": "subscription", "customer": a.stripe_cus,
                       "quantity": ev.seats, "status": "active"}, prev={"quantity": prev_q})
            prev_q = ev.seats
        notice = next((r.notice_day for r in a.renewals if r.hidden_outcome == "churned" and r.notice_day), None)
        if notice and notice <= EXPORT_DATE - timedelta(days=1):
            add_event("customer.subscription.updated", notice,
                      {"id": a.stripe_sub, "object": "subscription", "customer": a.stripe_cus,
                       "cancel_at_period_end": True}, prev={"cancel_at_period_end": False})
        if a.churned:
            add_event("customer.subscription.deleted", a.end_date,
                      {"id": a.stripe_sub, "object": "subscription", "customer": a.stripe_cus,
                       "status": "canceled"}, hour=2)

        # ---- current subscription snapshot ---------------------------------
        as_of = min(EXPORT_DATE, (a.end_date or EXPORT_DATE) - timedelta(days=1))
        ps, pe = _period(a, as_of)
        pending_cancel = bool(notice and notice <= EXPORT_DATE and not a.churned)
        nxt = next((r.day for r in a.renewals if r.day > EXPORT_DATE), None)
        sub = {
            "id": a.stripe_sub, "object": "subscription", "customer": a.stripe_cus,
            "status": "canceled" if a.churned else ("past_due" if open_invoice else "active"),
            "start_date": int(created.timestamp()), "quantity": a.seats_at(as_of),
            "plan": {"interval": "year" if a.billing == "annual" else "month",
                     "amount": int(round(a.unit_price * 100)), "currency": "usd"},
            "current_period_start": int(at(ps).timestamp()), "current_period_end": int(at(pe).timestamp()),
            "cancel_at_period_end": bool(pending_cancel or (a.churned and notice)),
            "cancel_at": int(at(nxt).timestamp()) if pending_cancel and nxt else None,
            "canceled_at": int(at(notice).timestamp()) if notice and (a.churned or pending_cancel) else None,
            "ended_at": int(at(a.end_date).timestamp()) if a.churned else None,
            "_synced_at": iso_z(EXPORT_AT - timedelta(hours=3))}
        out["subscriptions"].append(sub)

    # test-mode / internal noise
    for _ in range(max(1, int(MESS["stripe_test_customers"] * len(accs)))):
        cid = rid(rng, "cus_", 14)
        out["customers"].append({
            "id": cid, "object": "customer", "created": int(at(WINDOW_START + timedelta(days=int(rng.integers(0, 900)))).timestamp()),
            "name": f"Test Co {int(rng.integers(1, 99))}", "email": f"test+{int(rng.integers(1, 999))}@{t.csm_domain}",
            "livemode": False, "metadata": {}, "delinquent": False, "_synced_at": iso_z(EXPORT_AT - timedelta(hours=3))})
        links.append((t.tenant_id, "stripe_customer", cid, "NOISE"))
    out["events"] = events
    return out
