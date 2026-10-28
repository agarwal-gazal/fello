"""Entity resolution: one account_id across Salesforce, Stripe, Zendesk, product analytics and Gmail.

    DATABASE_URL=... python -m resolve.resolve

Design (each point is a decision to defend):

* The SPINE is Salesforce: it is the only system that is meant to hold one row per customer. But Salesforce itself has
  duplicates, so the spine is first de-duplicated (same website domain; same normalised name with no domain conflict;
  very close name AND a shared contact domain). The canonical account is the earliest-created record; its Salesforce Id
  is the account_id everything else points at.
* Every other record collects CANDIDATES from independent kinds of evidence, ordered by how much they can be trusted:
      key      an explicit foreign id (Stripe metadata.salesforce_id, Zendesk external_id, workspace crm_account_id)  1.00
      domain   a company email/website domain owned by exactly one account                                           .90 to .95
      person   an exact email address belonging to a known contact / product user (works for gmail.com addresses)    .85 to .90
      vote     most common company domain among a workspace's users or an organisation's requesters                  .80
      name     normalised-name equality (.85), then fuzzy >= .90 with a clear margin over the runner-up (.70)
  The highest-confidence candidate wins. If a different account also has a serious candidate (>= .70) the link is
  flagged `conflict`, so disagreements are visible instead of silently resolved.
* Guards against the two classic failure modes:
    - consumer mail domains (gmail.com...) and the tenant's own staff domains never identify a company;
    - a domain claimed by more than one account is ambiguous and is not used.
    Near-identical names of DIFFERENT companies are kept apart because names are compared as whole normalised strings and
    a conflicting website domain vetoes a name match.
* Unresolved records are KEPT with a reason, never forced onto an account: a wrong link poisons an account's features,
  a missing link only costs some coverage.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict

import pandas as pd

from .io import read_df, write_df
from .names import NameIndex, edit_distance, name_key, similarity, slug_key

CONF = {"key": 1.0, "domain_website": .95, "domain_contact": .90, "person_contact": .90, "person_user": .85,
        "vote": .80, "name": .85, "name_fuzzy": .70, "domain_linked": .85,
        # a personal (gmail.com...) address names a PERSON who may move employer or appear at two companies:
        # weaker than company evidence, so a workspace's user-domain vote or a name match can overrule it
        "person_contact_freemail": .75, "person_user_freemail": .75}
WEAK = .85               # below this, also consult the name
FUZZY_MIN, FUZZY_MARGIN = .90, .05
SERIOUS = .70            # a competing candidate at or above this flags a conflict

DDL = """
drop schema if exists er cascade;
create schema er;
create table er.sf_merge (tenant_id text, sf_id text, account_id text, method text, confidence numeric,
                          primary key (tenant_id, sf_id));
create table er.account  (tenant_id text, account_id text, name text, website_domain text, n_sf_records int,
                          primary key (tenant_id, account_id));
create table er.link     (tenant_id text, source_entity text, source_record_id text, account_id text,
                          method text, confidence numeric, conflict boolean, reason text,
                          primary key (tenant_id, source_entity, source_record_id));
"""


# ------------------------------------------------------------------------------------------------
class UnionFind:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


class Tenant:
    """All resolution state for one tenant."""

    def __init__(self, tid, acc, con, staff, free):
        self.tid, self.free, self.staff = tid, free, staff
        self.acc = acc.set_index("sf_id")
        self.con = con
        self.merge: dict[str, tuple[str, str, float]] = {}          # sf_id -> (canonical, method, conf)
        self.canon: dict[str, dict] = {}
        self.dom_owner: dict[str, dict[str, str]] = defaultdict(dict)   # domain -> {account_id: 'website'|'contact'}
        self.person: dict[str, dict[str, str]] = defaultdict(dict)      # email -> {account_id: 'person_contact'|'person_user'}
        self.names: NameIndex | None = None
        self.stats = Counter()

    # ------------------------------------------------------------------ spine
    def dedupe_spine(self):
        a = self.acc
        uf = UnionFind(list(a.index))
        edges: list[tuple[str, str, str]] = []
        cdom = (self.con.dropna(subset=["email_domain"])
                .query("email_domain not in @self.free and email_domain not in @self.staff")
                .groupby("sf_account_id")["email_domain"].agg(set).to_dict())

        def conflict(x, y):
            wx, wy = a.at[x, "website_domain"], a.at[y, "website_domain"]
            return pd.notna(wx) and pd.notna(wy) and wx != wy

        # R1: same website domain
        for _, grp in a.dropna(subset=["website_domain"]).groupby("website_domain"):
            ids = list(grp.index)
            for o in ids[1:]:
                uf.union(ids[0], o)
                edges.append((ids[0], o, "domain"))
        # R2: same normalised name, unless the websites contradict each other
        keys = a["name"].map(name_key)
        for k, grp in a.groupby(keys):
            ids = list(grp.index)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    if not conflict(ids[i], ids[j]) and uf.find(ids[i]) != uf.find(ids[j]):
                        uf.union(ids[i], ids[j])
                        edges.append((ids[i], ids[j], "name"))
        # R3: typo duplicates. Names within ONE edit of each other (swapped/missing letter, space moved), long enough for that
        # to mean something, no contradicting website, and corroborated by a shared contact domain or the same CRM owner.
        # Different companies with similar names ('... Labs' vs '... Systems') are many edits apart and never qualify.
        idx = NameIndex(list(a.index), list(a["name"]))
        slugs = {i: slug_key(n) for i, n in a["name"].items()}
        for x in a.index:
            for score, y in idx.fuzzy(a.at[x, "name"], top=3):
                if y == x or uf.find(x) == uf.find(y) or conflict(x, y):
                    continue
                if len(slugs[x]) < 8 or edit_distance(slugs[x], slugs[y], cap=1) > 1:
                    continue
                shared_contact = bool(cdom.get(x, set()) & cdom.get(y, set()))
                # one substituted letter ('Ward' vs 'Yard') is how two real companies differ; typos are transpositions or
                # dropped/added letters. So a substitution needs contact-domain proof; the others also accept the same CRM owner.
                substitution = len(slugs[x]) == len(slugs[y]) and sum(c != d for c, d in zip(slugs[x], slugs[y])) == 1
                same_owner = a.at[x, "owner_id"] == a.at[y, "owner_id"]
                if shared_contact or (same_owner and not substitution):
                    uf.union(x, y)
                    edges.append((x, y, "typo"))
        # canonical = earliest created, ties by id
        groups = defaultdict(list)
        for i in a.index:
            groups[uf.find(i)].append(i)
        first_edge = {}
        for x, y, m in edges:
            first_edge.setdefault(y, m)
            first_edge.setdefault(x, m)
        for members in groups.values():
            canon = min(members, key=lambda i: (a.at[i, "created_at"], i))
            for m in members:
                self.merge[m] = (canon, "self" if m == canon else first_edge.get(m, "name"), 1.0 if m == canon else .9)
            self.canon[canon] = dict(members=members, name=a.at[canon, "name"],
                                     website_domain=next((a.at[m, "website_domain"] for m in sorted(members, key=lambda i: a.at[i, "created_at"])
                                                          if pd.notna(a.at[m, "website_domain"])), None))
        self.stats["sf_records"] = len(a)
        self.stats["accounts"] = len(self.canon)

    # ------------------------------------------------------------------ evidence indexes
    def build_indexes(self):
        for canon, info in self.canon.items():
            for m in info["members"]:
                w = self.acc.at[m, "website_domain"]
                if pd.notna(w) and w not in self.free and w not in self.staff:
                    self.dom_owner[w][canon] = "website"
        for r in self.con.itertuples():
            canon = self.merge.get(r.sf_account_id, (None,))[0]
            if canon is None:
                continue
            if pd.notna(r.email):
                self.person[r.email][canon] = "person_contact"
            d = r.email_domain
            if pd.notna(d) and d not in self.free and d not in self.staff:
                self.dom_owner[d].setdefault(canon, "contact")
        self.names = NameIndex(list(self.canon), [v["name"] for v in self.canon.values()])

    def learn_domain(self, domain, account):
        """A company domain seen on a record we already linked (e.g. an alias domain only Zendesk knows) becomes evidence for
        later records. Only unclaimed domains are learned: never create an ambiguity, never overwrite an owner."""
        if pd.isna(domain) or not domain or domain in self.free or domain in self.staff or domain in self.dom_owner:
            return
        self.dom_owner[domain][account] = "linked"

    # ------------------------------------------------------------------ evidence -> candidates
    def c_key(self, sf_id):
        if pd.notna(sf_id) and sf_id in self.merge:
            return [(self.merge[sf_id][0], "key", CONF["key"])]
        return []

    def domain_owner(self, d):
        """-> (account, kind) when exactly one account owns the domain; (None, reason) otherwise."""
        if pd.isna(d) or not d:
            return None, "no_domain"
        if d in self.free:
            return None, "free_mail"
        if d in self.staff:
            return None, "internal"
        owners = self.dom_owner.get(d, {})
        if len(owners) == 1:
            (acct, kind), = owners.items()
            return acct, kind
        return None, "ambiguous_domain" if owners else "unknown_domain"

    def c_domain(self, d):
        acct, kind = self.domain_owner(d)
        if acct:
            return [(acct, f"domain_{kind}", CONF[f"domain_{kind}"])]
        return []

    def c_person(self, email):
        if pd.isna(email):
            return []
        owners = self.person.get(email, {})
        if len(owners) == 1:
            (acct, kind), = owners.items()
            if self.is_free_address(email):
                kind += "_freemail"
            return [(acct, kind, CONF[kind])]
        return []

    def is_free_address(self, email):
        return str(email).split("@")[-1] in self.free

    def c_name(self, name, slug=False):
        if pd.isna(name) or not name:
            return []
        ex = self.names.exact_slug(name) if slug else self.names.exact(name)
        if len(ex) == 1:
            return [(ex[0], "name", CONF["name"])]
        if ex:
            return []                                    # several accounts share this name: ambiguous, do not guess
        if slug:
            return []
        fz = self.names.fuzzy(name)
        if fz and fz[0][0] >= FUZZY_MIN and (len(fz) == 1 or fz[0][0] - fz[1][0] >= FUZZY_MARGIN):
            return [(fz[0][1], "name_fuzzy", CONF["name_fuzzy"])]
        return []

    def c_vote(self, domains: Counter):
        """Majority company domain among the people attached to a workspace / organisation."""
        vals = [(c, d) for d, c in domains.items() if self.domain_owner(d)[0]]
        if not vals:
            return []
        vals.sort(reverse=True)
        top_c, top_d = vals[0]
        if len(vals) > 1 and vals[1][0] == top_c and self.domain_owner(vals[1][1])[0] != self.domain_owner(top_d)[0]:
            return []                                    # tie between different accounts
        return [(self.domain_owner(top_d)[0], "vote", CONF["vote"])]


def decide(cands):
    """Highest confidence wins; a serious competitor on another account marks the link as a conflict."""
    if not cands:
        return None
    best: dict[str, tuple[str, float]] = {}
    for acct, method, conf in cands:
        if acct not in best or conf > best[acct][1]:
            best[acct] = (method, conf)
    ranked = sorted(best.items(), key=lambda kv: -kv[1][1])
    top_acct, (method, conf) = ranked[0]
    conflict = len(ranked) > 1 and ranked[1][1][1] >= SERIOUS
    return top_acct, method, conf, conflict


def unresolved_reason(t: Tenant, domain, email=None, default="no_evidence"):
    if pd.notna(domain):
        if domain in t.staff:
            return "internal"
        if domain in t.free:
            return "free_mail_no_person_match"
        if domain in t.dom_owner and len(t.dom_owner[domain]) > 1:
            return "ambiguous_domain"
    return default


# ------------------------------------------------------------------------------------------------
def resolve_tenant(t: Tenant, d: dict) -> list[dict]:
    out: list[dict] = []

    def emit(entity, rid, res, reason=None):
        if res:
            out.append(dict(tenant_id=t.tid, source_entity=entity, source_record_id=rid, account_id=res[0],
                            method=res[1], confidence=res[2], conflict=res[3], reason=None))
        else:
            out.append(dict(tenant_id=t.tid, source_entity=entity, source_record_id=rid, account_id=None,
                            method="unresolved", confidence=None, conflict=False, reason=reason or "no_evidence"))

    # ---- Salesforce records themselves (duplicates map to the canonical account)
    for sf_id, (canon, method, conf) in t.merge.items():
        out.append(dict(tenant_id=t.tid, source_entity="salesforce_account", source_record_id=sf_id, account_id=canon,
                        method=method, confidence=conf, conflict=False, reason=None))

    # ---- Stripe customers
    for r in d["stripe"].itertuples():
        if r.is_test == "t" or r.is_test is True or str(r.is_test).lower() in ("t", "true"):
            emit("stripe_customer", r.customer_id, None, "excluded_test_mode")
            continue
        c = t.c_key(r.sf_id_hint) + t.c_domain(r.email_domain) + t.c_person(r.email)
        if not c or max(x[2] for x in c) < WEAK:
            c += t.c_name(r.name)
        res = decide(c)
        if res and res[2] >= WEAK:
            t.learn_domain(r.email_domain, res[0])
        emit("stripe_customer", r.customer_id, res, unresolved_reason(t, r.email_domain))

    # ---- Zendesk organisations (vote over the people who file tickets under them)
    req_votes: dict[str, Counter] = defaultdict(Counter)
    for r in d["tickets"].dropna(subset=["org_id"]).itertuples():
        if pd.notna(r.requester_domain):
            req_votes[r.org_id][r.requester_domain] += 1
    for r in d["zd_org"].itertuples():
        c = t.c_key(r.sf_id_hint)
        for dom in (r.domains or []):
            c += t.c_domain(dom)
        c += t.c_vote(req_votes.get(r.org_id, Counter()))
        if not c or max(x[2] for x in c) < WEAK:
            c += t.c_name(r.name)
        res = decide(c)
        if res and res[2] >= WEAK:
            for dom in (r.domains or []):
                t.learn_domain(dom, res[0])
        emit("zendesk_org", r.org_id, res, "no_evidence")

    # ---- Product workspaces (vote over the people who use them)
    ws_votes: dict[str, Counter] = defaultdict(Counter)
    for r in d["users"].itertuples():
        if pd.notna(r.email_domain):
            ws_votes[r.workspace_id][r.email_domain] += 1
    ws_account: dict[str, str] = {}
    for r in d["ws"].itertuples():
        if r.is_internal in ("t", True, "true"):
            emit("product_workspace", r.workspace_id, None, "excluded_internal")
            continue
        c = (t.c_key(r.sf_id_hint) + t.c_person(r.owner_email) + t.c_domain(r.owner_domain)
             + t.c_vote(ws_votes.get(r.workspace_id, Counter())))
        if not c or max(x[2] for x in c) < WEAK:
            c += t.c_name(r.name, slug=True) or t.c_name(r.name)
        res = decide(c)
        if res:
            ws_account[r.workspace_id] = res[0]
            if res[2] >= WEAK:
                t.learn_domain(r.owner_domain, res[0])
        emit("product_workspace", r.workspace_id, res, unresolved_reason(t, r.owner_domain))

    # product users of linked workspaces extend the 'person' index (a gmail.com ticket requester who also uses the product)
    for r in d["users"].itertuples():
        a = ws_account.get(r.workspace_id)
        if a and pd.notna(r.email):
            t.person[r.email].setdefault(a, "person_user")

    # ---- Zendesk tickets: organisation link first, then the requester as a person, then the requester's domain
    org_account = {x["source_record_id"]: x["account_id"] for x in out
                   if x["source_entity"] == "zendesk_org" and x["account_id"]}
    for r in d["tickets"].itertuples():
        c = []
        if pd.notna(r.org_id) and r.org_id in org_account:
            c.append((org_account[r.org_id], "key", CONF["key"]))
        c += t.c_person(r.requester_email) + t.c_domain(r.requester_domain)
        emit("zendesk_ticket", r.ticket_id, decide(c), unresolved_reason(t, r.requester_domain))

    # ---- Gmail messages: the counterparty (for a bounce, the address that failed)
    for r in d["gmail"].itertuples():
        if r.is_internal in ("t", True, "true"):
            emit("gmail_message", r.message_id, None, "internal")
            continue
        c = t.c_person(r.counterparty_email) + t.c_domain(r.counterparty_domain)
        emit("gmail_message", r.message_id, decide(c), unresolved_reason(t, r.counterparty_domain))
    return out


# ------------------------------------------------------------------------------------------------
def run(db_url: str) -> pd.DataFrame:
    acc = read_df(db_url, "select tenant_id, sf_id, name, website_domain, owner_id, created_at from stg.sf_account")
    con = read_df(db_url, "select tenant_id, sf_account_id, email, email_domain from stg.sf_contact")
    stripe = read_df(db_url, "select tenant_id, customer_id, name, email, email_domain, sf_id_hint, is_test from stg.stripe_customer")
    zd_org = read_df(db_url, "select tenant_id, org_id::text as org_id, name, domains::text as domains, sf_id_hint from stg.zd_org")
    zd_org["domains"] = zd_org["domains"].map(lambda s: [x.strip('"') for x in str(s).strip("{}").split(",") if x] if pd.notna(s) else [])
    tickets = read_df(db_url, "select tenant_id, ticket_id::text as ticket_id, org_id::text as org_id, requester_email, requester_domain from stg.zd_ticket")
    ws = read_df(db_url, "select tenant_id, workspace_id, name, owner_email, owner_domain, sf_id_hint, is_internal from stg.product_workspace")
    users = read_df(db_url, "select tenant_id, user_id, workspace_id, email, email_domain from stg.product_user")
    gmail = read_df(db_url, "select tenant_id, message_id, counterparty_email, counterparty_domain, is_internal from stg.gmail_message")
    staff = read_df(db_url, "select tenant_id, domain from stg.tenant_staff_domain")
    free = set(read_df(db_url, "select domain from stg.free_mail_domain")["domain"])

    all_links, accounts, merges = [], [], []
    for tid in sorted(acc.tenant_id.unique()):
        sub = lambda df: df[df.tenant_id == tid]
        t = Tenant(tid, sub(acc).drop(columns="tenant_id"), sub(con), set(sub(staff).domain), free)
        t.dedupe_spine()
        t.build_indexes()
        links = resolve_tenant(t, dict(stripe=sub(stripe), zd_org=sub(zd_org), tickets=sub(tickets), ws=sub(ws),
                                       users=sub(users), gmail=sub(gmail)))
        all_links += links
        merges += [dict(tenant_id=tid, sf_id=k, account_id=v[0], method=v[1], confidence=v[2]) for k, v in t.merge.items()]
        accounts += [dict(tenant_id=tid, account_id=k, name=v["name"], website_domain=v["website_domain"], n_sf_records=len(v["members"]))
                     for k, v in t.canon.items()]
        print(f"  {tid}: {t.stats['sf_records']} Salesforce records -> {t.stats['accounts']} accounts; {len(links):,} links", flush=True)

    link_df = pd.DataFrame(all_links)
    link_df["confidence"] = link_df["confidence"].astype(float)
    psql_ddl = DDL
    from ingest.load_raw import psql
    psql(db_url, psql_ddl)
    write_df(db_url, "er.sf_merge", pd.DataFrame(merges), "select 1")
    write_df(db_url, "er.account", pd.DataFrame(accounts), "select 1")
    write_df(db_url, "er.link", link_df, "select 1")
    return link_df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args(argv)
    if not args.database_url:
        sys.exit("set DATABASE_URL or pass --database-url")
    t0 = time.time()
    links = run(args.database_url)
    print(f"\nlinks by entity and method:")
    print(links.groupby(["source_entity", "method"]).size().unstack(fill_value=0).to_string())
    print(f"\nresolved {links.account_id.notna().mean():.1%} of {len(links):,} records in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
