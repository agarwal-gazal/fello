"""Zendesk, product-analytics and Gmail emitters.

Each reads the hidden health curve with its OWN lag and noise, so signals do not move
in lock-step. Shared tenant incidents (outages, releases) create confounders that look
like account-level trouble but are not.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from .config import (EMAIL_IN, EMAIL_OUT, EXPORT_AT, MESS, N_WEEKS, TICKET_C, USAGE_C, USAGE_EXP,
                     WINDOW_START, TenantConfig)

USAGE_SCALE = [1.0]          # mutable so the CLI can raise product-event volume (--usage-scale)
from .mess import (UTC, arrived, at, business_times, from_epoch, iso_naive, iso_z, sync_time)
from .names import FIRST, LAST, PERSONAL, messy_variant, slug
from .world import Account, rid, week_date, week_idx

# --------------------------------------------------------------------------------
# Tenant calendar: seasonality + shared incidents
# --------------------------------------------------------------------------------


def make_calendar(t: TenantConfig, rng) -> dict:
    season = np.ones(N_WEEKS)
    for w in range(N_WEEKS):
        d = week_date(w)
        if (d.month == 12 and d.day >= 20) or (d.month == 1 and d.day <= 3):
            season[w] = .55
        elif d.month == 11 and 22 <= d.day <= 29:
            season[w] = .85
        elif d.month == 8:
            season[w] = .90
    outage = rng.choice(np.arange(20, N_WEEKS - 4), size=3, replace=False)
    release = rng.choice(np.arange(10, N_WEEKS - 4), size=4, replace=False)
    ticket_m, usage_m = np.ones(N_WEEKS), np.ones(N_WEEKS)
    ticket_m[outage] = 3.2
    usage_m[outage] = .6
    usage_m[release] = 1.25
    ticket_m[release] = 1.3
    return dict(season=season, ticket=ticket_m, usage=usage_m, outage=set(int(x) for x in outage),
                release=set(int(x) for x in release))


def active_mask(a: Account) -> np.ndarray:
    m = np.zeros(N_WEEKS, dtype=bool)
    m[a.first_week():a.last_week() + 1] = True
    return m


def _requesters(rng, a: Account) -> list[tuple[str, str]]:
    people = [(f"{c.first} {c.last}", c.email, c.left_week) for c in a.contacts]
    for _ in range(min(8, int(a.seats0 ** .4))):
        f, l = str(rng.choice(FIRST)), str(rng.choice(LAST))
        dom = a.alias_domain if (a.alias_domain and rng.random() < .4) else a.domain
        people.append((f"{f} {l}", f"{f.lower()}.{l.lower()}@{dom}", None))
    return people


# =============================================================================
# Zendesk
# =============================================================================
CATS_OK = dict(how_to=.34, feature_request=.18, bug=.14, performance=.05, integration=.12, billing=.08,
               security=.04, data_export=.05, cancellation=0.0)
CATS_BAD = dict(how_to=.10, feature_request=.04, bug=.28, performance=.18, integration=.10, billing=.10,
                security=.03, data_export=.09, cancellation=.08)
CATS = list(CATS_OK)
TEXT = {
    "how_to": [("How do I set this up for my team?", "We're trying to configure {f} for a new group and can't find the right setting."),
               ("Question about {f}", "Could someone point me to the docs for {f}? We want to roll it out next week.")],
    "feature_request": [("Feature request: {f}", "It would help us a lot if {f} supported bulk actions and saved views.")],
    "bug": [("{f} is not working", "Since yesterday {f} throws an error and we can't complete our workflow."),
            ("Unexpected error in {f}", "We keep hitting an error in {f}. Screenshots attached.")],
    "performance": [("{f} is very slow", "Pages are timing out whenever we use {f}. This affects the whole team."),
                    ("Performance degradation", "Everything has been sluggish for days, especially {f}.")],
    "integration": [("Integration sync failing", "Our integration stopped syncing with {f} after a token change.")],
    "billing": [("Question about our invoice", "The latest invoice doesn't match our seat count. Can you check?"),
                ("Payment method update", "Our card changed, how do we update billing details?")],
    "security": [("Security questionnaire", "Our InfoSec team needs your SOC 2 report and data-retention policy.")],
    "data_export": [("Need a full data export", "Please export all our data in CSV. We need it by end of week.")],
    "cancellation": [("Renewal and alternatives", "We are reviewing whether to renew and are comparing alternatives."),
                     ("Considering cancelling", "Leadership asked us to cut tools this year. Please explain how cancellation works.")],
    "outage": [("Platform down?", "Nobody on our team can log in right now. Is there an incident?")],
}
NEG_TAIL = ("This is the third time we've raised this.", "Honestly we're getting frustrated.",
            "We were promised a fix weeks ago.", "Our leadership is asking whether this tool is worth it.",
            "Responses have been too slow.")
POS_TAIL = ("Thanks, the team has been happy with the product.", "Appreciate the quick help last time.")


PLANTED_TICKETS = {      # days before renewal -> category, for the two 'lost' cases
    "lost_A": ((98, "performance"), (77, "bug"), (56, "cancellation"), (35, "performance")),
    "lost_B": ((84, "bug"), (63, "performance"), (42, "cancellation")),
}


def _planted_tickets(a: Account) -> list:
    """Explicit pain in Zendesk for the planted lost renewals ("the signals were sitting right there")."""
    out = []
    for days, cat in PLANTED_TICKETS.get(a.role, ()):
        d = a.planted_renewal - timedelta(days=days)
        out.append((week_idx(d), .2, at(d, 14.0, a.tz), cat))
    return out


def emit_zendesk(t: TenantConfig, accs: list[Account], rng, links: list, cal: dict):
    out = {"organizations": [], "tickets": []}
    tid = 10_000 + int(rng.integers(0, 5000))
    oid = 1_000_000 + int(rng.integers(0, 9000)) * 10
    base = np.array(list(CATS_OK.values()))
    bad = np.array(list(CATS_BAD.values()))

    for a in accs:
        if "zendesk" in a.missing_sources:
            continue
        a.has_zd_org = rng.random() < t.zd_org_rate and "no_zd_org" not in a.planted_mess
        if a.has_zd_org:
            oid += int(rng.integers(1, 9))
            a.zd_org_id = oid
            dn = [a.domain] + ([a.alias_domain] if a.alias_domain else [])
            if rng.random() < .15:
                dn = []
            out["organizations"].append({
                "id": oid, "name": messy_variant(rng, a.base_name, a.legal), "domain_names": dn,
                "external_id": a.sf_id if rng.random() < .40 else None,
                "created_at": iso_z(at(max(a.contract_start, WINDOW_START))),
                "_synced_at": iso_z(EXPORT_AT - timedelta(hours=4))})
            links.append((t.tenant_id, "zendesk_org", str(oid), a.true_id))

        mask = active_mask(a)
        h = a.h_obs(2)
        seats_w = a.seats_by_week()
        lam = (TICKET_C * t.ticket_mult * seats_w ** .4 * (.25 + 2.4 * (1 - h) ** 1.5)
               * cal["ticket"] * min(a.act_mult, 1.3) ** .5)
        lam = np.where(mask, lam, 0.0)
        counts = rng.poisson(lam)
        people = _requesters(rng, a)
        feat_names = [f[0] for f in t.features]
        items = []                                  # (week, health, datetime, forced_category)
        for w in np.nonzero(counts)[0]:
            for ts in business_times(rng, int(counts[w]), int(w), a.tz):
                items.append((int(w), float(h[w]), from_epoch(float(ts)), None))
        items += _planted_tickets(a)
        if True:
            for (w, hw, dt, cat_force) in items:
                if dt > EXPORT_AT:
                    continue
                probs = base * hw + bad * (1 - hw)
                if hw < .35:
                    probs = probs.copy()
                    probs[CATS.index("cancellation")] += .06
                probs = probs / probs.sum()
                if cat_force:
                    cat = cat_force
                else:
                    cat = "outage" if (int(w) in cal["outage"] and rng.random() < .7) else CATS[int(rng.choice(len(CATS), p=probs))]
                subj_t, body_t = TEXT[cat][int(rng.integers(0, len(TEXT[cat])))]
                feat = str(rng.choice(feat_names)).replace("_", " ")
                body = body_t.format(f=feat)
                if rng.random() < (1 - hw) ** 1.2 and cat != "outage":
                    body += " " + str(rng.choice(NEG_TAIL))
                elif rng.random() < hw * .25:
                    body += " " + str(rng.choice(POS_TAIL))
                # requester
                avail = [p for p in people if p[2] is None or int(w) < p[2]] or people
                name, email, _ = avail[int(rng.integers(0, len(avail)))]
                r = rng.random()
                if r < MESS["zd_personal_email"]:
                    email = f"{name.split()[0].lower()}{name.split()[-1].lower()}@{rng.choice(PERSONAL)}"
                elif r < MESS["zd_personal_email"] + .04:
                    email = f"support@{a.domain}"
                prio = ("urgent" if cat in ("outage",) and rng.random() < .5 else
                        "high" if cat in ("bug", "performance", "outage", "security", "cancellation") and rng.random() < .5
                        else "normal" if rng.random() < .8 else "low")
                tid += int(rng.integers(1, 4))
                mult = {"urgent": .5, "high": .8, "normal": 1.0, "low": 1.6}[prio]
                solve_h = float(rng.lognormal(np.log(18), .9)) * mult
                solved = dt + timedelta(hours=solve_h)
                solved_ok = solved <= EXPORT_AT
                reopened = rng.random() < (.03 + .08 * (1 - hw))
                sat = {"score": "unoffered"}
                if solved_ok and rng.random() < .55:
                    sat = {"score": "offered"}
                    if rng.random() < .35:
                        good = rng.random() < (.45 + .5 * hw - (.25 if reopened else 0))
                        sat = {"score": "good" if good else "bad"}
                linked = a.has_zd_org and rng.random() > MESS["zd_ticket_unlinked"]
                rec = {
                    "id": tid, "subject": subj_t.format(f=feat), "description": body, "status": "solved" if solved_ok else "open",
                    "priority": prio, "channel": str(rng.choice(["email", "web", "chat"], p=[.6, .3, .1])),
                    "requester_email": email, "requester_name": name,
                    "organization_id": a.zd_org_id if linked else None,
                    "tags": [cat] + (["reopened"] if reopened else []) + (["escalated"] if prio == "urgent" else []),
                    "created_at": iso_z(dt), "updated_at": iso_z(solved if solved_ok else dt),
                    "solved_at": iso_z(solved) if solved_ok else None,
                    "first_reply_minutes": int(rng.lognormal(np.log(90), 1.0)),
                    "satisfaction_rating": sat}
                avail_dt = solved if solved_ok else dt
                s = sync_time(rng, avail_dt, p_late=.03, late_days=(3, 30))
                if s <= EXPORT_AT:
                    out["tickets"].append({**rec, "_synced_at": iso_z(s)})
                    links.append((t.tenant_id, "zendesk_ticket", str(tid), a.true_id))
                if solved_ok and rng.random() < MESS["zd_dup_version"]:      # earlier version also exported
                    old = {**rec, "status": "open", "updated_at": iso_z(dt + timedelta(hours=2)),
                           "solved_at": None, "satisfaction_rating": {"score": "unoffered"}}
                    out["tickets"].append({**old, "_synced_at": iso_z(min(dt + timedelta(hours=float(rng.uniform(3, 30))), EXPORT_AT))})
                    links.append((t.tenant_id, "zendesk_ticket", str(tid), a.true_id))
    return out


# =============================================================================
# Product analytics
# =============================================================================
def emit_product(t: TenantConfig, accs: list[Account], rng, links: list, cal: dict):
    out = {"events": [], "workspaces": [], "users": []}
    fnames = [f[0] for f in t.features]
    fw = np.array([f[1] for f in t.features], dtype=float)
    adv = np.array([f[2] for f in t.features], dtype=bool)
    platforms, pp = ("web", "ios", "android"), (.82, .10, .08)
    counter = 0
    events = out["events"]

    for a in accs:
        if "product" in a.missing_sources:
            continue
        nws = 2 if rng.random() < .10 else 1
        for k in range(nws):
            wid = rid(rng, "ws_", 10)
            a.workspaces.append(wid)
            if "workspace_name_mismatch" in a.planted_mess:
                wname = f"{slug(str(rng.choice(['north', 'harbor', 'summit'])))}-ops-{int(rng.integers(10, 99))}"
            else:
                wname = f"{slug(a.base_name)}" if rng.random() < .5 else messy_variant(rng, a.base_name, a.legal)
            if k == 1:
                wname += " (staging)" if rng.random() < .5 else " - Team B"
            out["workspaces"].append({
                "workspace_id": wid, "name": wname,
                "owner_email": a.contacts[0].email, "created_at": iso_z(at(max(a.contract_start, WINDOW_START))),
                "plan": "enterprise" if a.segment == "ent" else "growth" if a.segment == "mid" else "starter",
                "crm_account_id": a.sf_id if rng.random() < .35 else None,
                "_synced_at": iso_z(EXPORT_AT - timedelta(hours=5))})
            links.append((t.tenant_id, "product_workspace", wid, a.true_id))
        ws_primary = a.workspaces[0]

        # user pool
        pool = []
        for c in a.contacts:
            pool.append(dict(uid=rid(rng, "u_", 10), email=c.email, left=c.left_week, role=c.role))
        for _ in range(max(0, min(25, int(a.seats0 ** .6)) - len(pool))):
            f, l = str(rng.choice(FIRST)), str(rng.choice(LAST))
            pool.append(dict(uid=rid(rng, "u_", 10), email=f"{f.lower()}.{l.lower()}@{a.domain}", left=None, role="user"))
        for u in pool:
            out["users"].append({
                "user_id": u["uid"], "workspace_id": ws_primary, "email": u["email"], "role": u["role"],
                "created_at": iso_z(at(max(a.contract_start, WINDOW_START))),
                "deactivated_at": iso_z(at(week_date(u["left"]))) if u["left"] is not None else None,
                "_synced_at": iso_z(EXPORT_AT - timedelta(hours=5))})

        mask = active_mask(a)
        h = a.h_obs(1)
        seats_w = a.seats_by_week()
        lam = (USAGE_C * USAGE_SCALE[0] * t.usage_mult * a.act_mult * seats_w ** USAGE_EXP
               * (.05 + 1.3 * h ** 2) * cal["season"] * cal["usage"])
        lam = np.where(mask, lam, 0.0)
        w0 = a.first_week()
        if a.contract_start >= WINDOW_START:
            lam[w0:w0 + 6] *= 1.6                                   # onboarding burst
        counts = rng.poisson(lam)
        zipf = 1.0 / (np.arange(1, len(pool) + 1) ** .8)
        for w in np.nonzero(counts)[0]:
            n = int(counts[w])
            times = business_times(rng, n, int(w), a.tz)
            hw = float(h[w])
            probs = fw * np.where(adv, .3 + hw, 1.0)
            probs = probs / probs.sum()
            avail = np.array([(u["left"] is None or int(w) < u["left"]) for u in pool])
            uw = zipf * avail
            if uw.sum() == 0:
                uw = zipf
            uw = uw / uw.sum()
            for ts in times:
                dt = from_epoch(float(ts))
                if dt > EXPORT_AT:
                    continue
                counter += 1
                name = "user_login" if rng.random() < .22 else fnames[int(rng.choice(len(fnames), p=probs))]
                u = pool[int(rng.choice(len(pool), p=uw))]
                anon = rng.random() < .05
                ts_str = iso_z(dt)
                r = rng.random()
                if r < MESS["prod_naive_ts"]:
                    ts_str = iso_naive(dt, a.tz)
                elif r < MESS["prod_naive_ts"] + MESS["prod_future_ts"]:
                    ts_str = iso_z(dt + timedelta(days=float(rng.uniform(1, 3))))
                props = {"platform": platforms[int(rng.choice(3, p=pp))]}
                if name == "api_call":
                    props["status"] = int(rng.choice([200, 429, 500], p=[.96 - .1 * (1 - hw), .02 + .06 * (1 - hw), .02 + .04 * (1 - hw)]))
                rec = {"event_id": f"{t.tenant_id[4:7]}_{counter:08d}", "event": name,
                       "user_id": None if anon else u["uid"], "anonymous_id": rid(rng, "anon_", 8) if anon else None,
                       "workspace_id": None if rng.random() < MESS["prod_null_ws"] else (
                           a.workspaces[-1] if len(a.workspaces) > 1 and rng.random() < .2 else ws_primary),
                       "timestamp": ts_str, "properties": props}
                s = sync_time(rng, dt, p_late=MESS["prod_late"], late_days=(1, 10),
                              p_backfill=MESS["prod_backfill"], backfill_days=(15, 90))
                if s <= EXPORT_AT:
                    events.append({**rec, "_synced_at": iso_z(s)})
                    if rng.random() < MESS["prod_dup"]:
                        s2 = s + timedelta(minutes=float(rng.uniform(1, 400)))
                        if s2 <= EXPORT_AT:
                            events.append({**rec, "_synced_at": iso_z(s2)})

    # internal / QA traffic from the tenant's own staff
    n_int = int(MESS["prod_internal"] * len(events))
    iws = rid(rng, "ws_", 10)
    out["workspaces"].append({"workspace_id": iws, "name": f"{t.name} Internal QA", "owner_email": f"qa@{t.csm_domain}",
                              "created_at": iso_z(at(WINDOW_START)), "plan": "enterprise", "crm_account_id": None,
                              "_synced_at": iso_z(EXPORT_AT - timedelta(hours=5))})
    links.append((t.tenant_id, "product_workspace", iws, "NOISE"))
    iu = [rid(rng, "u_", 10) for _ in range(5)]
    for i, uid in enumerate(iu):
        out["users"].append({"user_id": uid, "workspace_id": iws, "email": f"qa{i}@{t.csm_domain}", "role": "admin",
                             "created_at": iso_z(at(WINDOW_START)), "deactivated_at": None,
                             "_synced_at": iso_z(EXPORT_AT - timedelta(hours=5))})
    for _ in range(n_int):
        counter += 1
        dt = from_epoch(float(business_times(rng, 1, int(rng.integers(0, N_WEEKS - 1)), 0.0)[0]))
        events.append({"event_id": f"{t.tenant_id[4:7]}_{counter:08d}", "event": str(rng.choice(fnames)),
                       "user_id": str(rng.choice(iu)), "anonymous_id": None, "workspace_id": iws,
                       "timestamp": iso_z(dt), "properties": {"platform": "web"},
                       "_synced_at": iso_z(min(dt + timedelta(hours=1), EXPORT_AT))})
    return out


# =============================================================================
# Gmail (CSM mailboxes)
# =============================================================================
OUT_SUBJECTS = ("Quick check-in", "Quarterly business review - scheduling", "Following up on our call",
                "New feature walkthrough", "How is the rollout going?", "Onboarding next steps")
REN_SUBJECTS = ("Your upcoming renewal", "Renewal discussion", "Planning for next term")
IN_SUBJECTS_OK = ("Question about reports", "Adding more seats", "Quick question", "Scheduling our next sync")
IN_SUBJECTS_BAD = ("Concerns about the platform", "Need to talk about pricing", "Issues our team keeps hitting",
                   "Reviewing our tooling budget")
SNIP_OUT = ("Hi {n}, wanted to check in and see how things are going. Do you have 20 minutes this week?",
            "Hi {n}, ahead of your renewal I'd love to walk through usage and plans for next year.",
            "Hi {n}, sharing a few new features I think your team will like. Free for a quick call?")
SNIP_IN_OK = ("Thanks {n}! Things are going well, happy to chat next week.",
              "Sounds good, let's do Thursday. We're also thinking of adding a few seats.")
SNIP_IN_MID = ("Thanks for reaching out. Can we push this to next month? Things are busy.",
               "Appreciate it. We'll get back to you after our planning cycle.")
SNIP_IN_BAD = ("Honestly we've been frustrated with reliability and are looking at other options.",
               "We need to discuss pricing before we commit to another year.")


def _hex(rng, n=16):
    return "".join("0123456789abcdef"[i] for i in rng.integers(0, 16, n))


def emit_gmail(t: TenantConfig, accs: list[Account], csms: list[dict], rng, links: list, cal: dict):
    out = {"messages": []}
    msgs = out["messages"]
    mailbox = {}
    for c in csms:
        if rng.random() < t.gmail_connected:
            if rng.random() < .55:
                conn = at(WINDOW_START + timedelta(days=int(rng.integers(0, 7))))
            else:
                conn = at(WINDOW_START + timedelta(days=int(rng.integers(30, 700))))
            mailbox[c["user_id"]] = dict(email=c["email"], name=c["name"], connected=conn, n=0)
    csm_by_id = {c["user_id"]: c for c in csms}
    cur = ["NOISE"]          # true account of the messages currently being emitted (answer key only)

    def push(mb_id, dt, from_name, from_addr, to, subject, snippet, inbound, thread):
        mb = mailbox[mb_id]
        if dt > EXPORT_AT:
            return
        if dt < mb["connected"]:                                    # bulk historical backfill
            s = mb["connected"] + timedelta(minutes=float(rng.uniform(1, 600)))
        else:
            s = sync_time(rng, dt, p_late=.02, late_days=(1, 5))
        if s > EXPORT_AT:
            return
        mb["n"] += 1
        mid = _hex(rng)
        links.append((t.tenant_id, "gmail_message", mid, cur[0]))
        msgs.append({
            "id": mid, "threadId": thread, "labelIds": ["INBOX"] if inbound else ["SENT"],
            "snippet": snippet, "internalDate": str(int(dt.timestamp() * 1000)),
            "payload": {"headers": [
                {"name": "From", "value": f"{from_name} <{from_addr}>"},
                {"name": "To", "value": ", ".join(to)}, {"name": "Subject", "value": subject}]},
            "_mailbox": mb["email"], "_synced_at": iso_z(s)})

    for a in accs:
        cur[0] = a.true_id
        mask = active_mask(a)
        h = a.h_obs(2)
        wtr = a.weeks_to_renewal()
        attn = 1.0 + 1.2 * ((wtr >= 0) & (wtr <= 16))
        lam_out = EMAIL_OUT[a.segment] * t.email_mult * attn * np.minimum(a.act_mult, 1.5)
        lam_in = EMAIL_IN[a.segment] * t.email_mult * (.10 + h ** 1.5) * np.minimum(a.act_mult, 1.5)
        lam_out, lam_in = np.where(mask, lam_out, 0.0), np.where(mask, lam_in, 0.0)
        c_out, c_in = rng.poisson(lam_out), rng.poisson(lam_in)
        champ = a.contacts[0]
        repl = None
        if champ.left_week is not None:
            f, l = str(rng.choice(FIRST)), str(rng.choice(LAST))
            repl = (f"{f} {l}", f"{f.lower()}.{l.lower()}@{a.domain}")
        for w in np.nonzero(c_out + c_in)[0]:
            w = int(w)
            owner = a.prev_csm_id if (a.reassign_week and w < a.reassign_week and a.prev_csm_id) else a.csm_id
            if owner not in mailbox:
                continue
            mb = mailbox[owner]
            hw = float(h[w])
            left = champ.left_week is not None and w >= champ.left_week
            active_c = [c for c in a.contacts if c.left_week is None or w < c.left_week]
            if left and w >= champ.left_week + 5 and repl:
                active_c = active_c + [type("R", (), dict(first=repl[0].split()[0], last=repl[0].split()[1],
                                                            email=repl[1]))()]

            def when():
                return from_epoch(float(business_times(rng, 1, w, 0.0)[0]))

            for _ in range(int(c_out[w])):
                target = champ if (left and w < champ.left_week + 12 and rng.random() < .6) else \
                    active_c[int(rng.integers(0, len(active_c)))] if active_c else champ
                subj = str(rng.choice(REN_SUBJECTS if wtr[w] <= 16 else OUT_SUBJECTS))
                th = _hex(rng)
                t0 = when()
                n = target.first
                push(owner, t0, mb["name"], mb["email"], [target.email], subj,
                     str(rng.choice(SNIP_OUT)).format(n=n), False, th)
                if target is champ and left:                              # bounce
                    push(owner, t0 + timedelta(minutes=2), "Mail Delivery Subsystem", "mailer-daemon@googlemail.com",
                         [mb["email"]], "Delivery Status Notification (Failure)",
                         f"Your message to {target.email} couldn't be delivered. The address no longer exists.", True, th)
                    continue
                if rng.random() < .04:                                    # out-of-office auto reply
                    push(owner, t0 + timedelta(minutes=3), f"{target.first} {target.last}", target.email, [mb["email"]],
                         "Automatic reply: " + subj, "I am out of the office with limited access to email.", True, th)
                    continue
                p_reply = float(np.clip(.04 + .92 * hw ** 1.2, .03, .95)) * (.6 if left else 1.0)
                if rng.random() < p_reply:
                    lat = float(rng.lognormal(np.log(6 + 100 * (1 - hw)), .8))
                    snip = SNIP_IN_OK if hw > .6 else SNIP_IN_MID if hw > .35 else SNIP_IN_BAD
                    t1 = t0 + timedelta(hours=lat)
                    push(owner, t1, f"{target.first} {target.last}", target.email, [mb["email"]], "Re: " + subj,
                         str(rng.choice(snip)).format(n=mb["name"].split()[0]), True, th)
                    if rng.random() < .6:
                        push(owner, t1 + timedelta(hours=float(rng.uniform(2, 30))), mb["name"], mb["email"],
                             [target.email], "Re: " + subj, "Thanks for the update. Let's lock a time and I'll send an invite.", False, th)
            for _ in range(int(c_in[w])):
                src = active_c[int(rng.integers(0, len(active_c)))] if active_c else champ
                if src is champ and left:
                    continue
                th = _hex(rng)
                subj = str(rng.choice(IN_SUBJECTS_OK if hw > .45 else IN_SUBJECTS_BAD))
                t0 = when()
                snip = SNIP_IN_OK if hw > .6 else SNIP_IN_MID if hw > .35 else SNIP_IN_BAD
                push(owner, t0, f"{src.first} {src.last}", src.email, [mb["email"]], subj,
                     str(rng.choice(snip)).format(n=mb["name"].split()[0]), True, th)
                if rng.random() < .9:
                    push(owner, t0 + timedelta(hours=float(rng.lognormal(np.log(5), .7))), mb["name"], mb["email"],
                         [src.email], "Re: " + subj, "Thanks for flagging this. Looking into it now and will update you shortly.",
                         False, th)

    # noise: internal mail, vendors, newsletters (no customer attached)
    cur[0] = "NOISE"
    for mb_id, mb in mailbox.items():
        for _ in range(int(MESS["gmail_noise"] * mb["n"]) + 3):
            dt = from_epoch(float(business_times(rng, 1, int(rng.integers(0, N_WEEKS - 1)), 0.0)[0]))
            kind = rng.random()
            if kind < .5:
                frm, name, subj, snip, inbound = f"manager@{t.csm_domain}", "CS Manager", "Forecast review", "Please update your renewal forecast by Friday.", True
            elif kind < .8:
                frm, name, subj, snip, inbound = "news@saas-weekly.example", "SaaS Weekly", "This week in customer success", "Top five retention plays for Q4.", True
            else:
                frm, name, subj, snip, inbound = mb["email"], mb["name"], "Notes for tomorrow", "Prep notes for team sync.", False
            push(mb_id, dt, name, frm, [mb["email"]] if inbound else [f"team@{t.csm_domain}"], subj, snip, inbound, _hex(rng))
    return out
