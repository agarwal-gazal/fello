"""The hidden world: accounts, latent health trajectories, renewals, contacts, CSMs.

Nothing here is observable by the downstream system. The five source emitters read
this world and write *noisy, lagged, partial* views of it. The ground truth is saved
separately so the churn model can be evaluated honestly.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from .config import (ARCHETYPES, CHURN_K, EXOGENOUS_CHURN, EXPORT_DATE, MESS,
                     N_PRE, N_WEEKS, SEG_ADJ, T_TOTAL, WINDOW_START, TenantConfig)
from .names import INDUSTRIES, NameFactory, PERSONAL, legal_name, person_email

ALPH = np.array(list("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def rid(rng: np.random.Generator, prefix: str, n: int) -> str:
    return prefix + "".join(ALPH[rng.integers(0, 62, n)])


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def week_idx(d: date) -> int:
    """Window week index (floor); negative before the window."""
    return (d - WINDOW_START).days // 7


def week_date(w: int) -> date:
    return WINDOW_START + timedelta(weeks=w)


def add_years(d: date, k: int) -> date:
    try:
        return d.replace(year=d.year + k)
    except ValueError:                      # Feb 29
        return d.replace(year=d.year + k, day=28)


@dataclass
class Contact:
    contact_id: str
    first: str
    last: str
    email: str
    title: str
    role: str                    # champion | decision_maker | user
    sf_champion_flag: bool
    personal_email: bool
    left_week: int | None = None  # window week the person left the customer (None = still there)


@dataclass
class SeatEvent:
    day: date
    seats: int
    kind: str                    # start | upgrade | downgrade_mid | renewal_expand | renewal_downgrade


@dataclass
class Renewal:
    k: int
    day: date
    hp: float                    # mean pre-renewal health (ground truth driver)
    p_churn: float
    outcome: str                 # renewed | expanded | downgraded | churned | pending
    hidden_outcome: str          # what *will* happen for pending renewals (truth only)
    exogenous: bool
    notice_day: date | None
    seats_before: int
    seats_after: int


@dataclass
class Account:
    tenant_id: str
    idx: int
    true_id: str
    base_name: str
    legal: str
    domain: str
    alias_domain: str | None
    industry: str
    segment: str
    employees: int
    tz: float
    seats0: int
    unit_price: float
    billing: str
    contract_start: date
    archetype: str
    act_mult: float
    missing_sources: set
    role: str = ""
    twin_of: str = ""
    override: dict = field(default_factory=dict)
    forced: dict = field(default_factory=dict)
    renewal_dates: list = field(default_factory=list)
    h: np.ndarray | None = None
    renewals: list = field(default_factory=list)
    seat_events: list = field(default_factory=list)
    end_date: date | None = None
    contacts: list = field(default_factory=list)
    champion_left_week: int | None = None
    csm_id: str = ""
    prev_csm_id: str = ""
    reassign_week: int | None = None
    # source ids (filled by the emitters / world builder)
    sf_id: str = ""
    sf_dup_id: str = ""
    stripe_cus: str = ""
    stripe_sub: str = ""
    zd_org_id: int = 0
    has_zd_org: bool = False
    workspaces: list = field(default_factory=list)
    # planted-case metadata (empty for normal accounts)
    planted_mess: tuple = ()
    planted_note: str = ""
    planted_renewal: date | None = None

    # ---- helpers -------------------------------------------------------------
    @property
    def churned(self) -> bool:
        return self.end_date is not None and self.end_date <= EXPORT_DATE

    def first_week(self) -> int:
        return max(0, week_idx(self.contract_start))

    def last_week(self) -> int:
        if self.end_date is None:
            return N_WEEKS - 1
        return min(N_WEEKS - 1, week_idx(self.end_date))

    def seats_at(self, d: date) -> int:
        days = [e.day for e in self.seat_events]
        i = bisect.bisect_right(days, d) - 1
        return self.seat_events[max(i, 0)].seats

    def seats_by_week(self) -> np.ndarray:
        return np.array([self.seats_at(week_date(w)) for w in range(N_WEEKS)], dtype=float)

    def h_obs(self, lag: int) -> np.ndarray:
        """Health as the *signal* sees it: lagged by `lag` weeks, for window weeks 0..N_WEEKS-1."""
        s = N_PRE - lag
        return self.h[s:s + N_WEEKS]

    def weeks_to_renewal(self) -> np.ndarray:
        """For each window week, weeks until the next renewal date (large if none)."""
        out = np.full(N_WEEKS, 999)
        rw = sorted(week_idx(r) for r in self.renewal_dates)
        for w in range(N_WEEKS):
            for r in rw:
                if r >= w:
                    out[w] = r - w
                    break
        return out


# --------------------------------------------------------------------------------
# Latent health
# --------------------------------------------------------------------------------
def ar1(rng, n, phi, sigma):
    e = rng.normal(0, sigma, n)
    out = np.empty(n)
    out[0] = e[0]
    for i in range(1, n):
        out[i] = phi * out[i - 1] + e[i]
    return out


def _anchor(rng, a: Account) -> int:
    cands = [week_idx(r) for r in a.renewal_dates if 10 <= week_idx(r) <= N_WEEKS + 30]
    if cands:
        return int(rng.choice(cands))
    return int(rng.integers(N_WEEKS - 60, N_WEEKS))


def simulate_health(rng, a: Account) -> np.ndarray:
    """Latent z over T_TOTAL weeks (index j <-> window week j - N_PRE); returns health in (0,1)."""
    t = np.arange(T_TOTAL) - N_PRE
    ov = a.override
    arch = a.archetype
    if arch == "steady_healthy":
        z = rng.normal(1.7, .5) + ar1(rng, T_TOTAL, .85, .22)
    elif arch == "steady_ok":
        z = rng.normal(.4, .5) + ar1(rng, T_TOTAL, .85, .25)
    elif arch == "quiet_loyal":
        z = rng.normal(.9, .4) + ar1(rng, T_TOTAL, .85, .20)
    elif arch == "expanding":
        z = rng.normal(1.3, .4) + 0.004 * np.clip(t, 0, None) + ar1(rng, T_TOTAL, .85, .22)
    elif arch == "volatile":
        z = rng.normal(.3, .5) + ar1(rng, T_TOTAL, .75, .55)
    elif arch == "slow_decline":
        anchor = ov.get("anchor", _anchor(rng, a))
        lead = ov.get("lead", int(rng.integers(18, 40)))
        onset = anchor - lead
        if onset > N_WEEKS - 3:
            onset = N_WEEKS - int(rng.integers(6, 30))
        L = ov.get("L", int(rng.integers(14, 36)))
        drop = ov.get("drop", rng.uniform(2.8, 4.2))
        base = ov.get("base", rng.normal(1.4, .5))
        z = base + ar1(rng, T_TOTAL, .85, .22) - drop * np.clip((t - onset) / L, 0, 1)
    elif arch == "sudden_drop":
        anchor = ov.get("anchor", _anchor(rng, a))
        step = anchor - ov.get("lead", int(rng.integers(6, 26)))
        step = min(step, N_WEEKS - 2)
        delta = ov.get("delta", rng.uniform(2.2, 3.6))
        base = ov.get("base", rng.normal(1.3, .5))
        z = base + ar1(rng, T_TOTAL, .85, .22)
        after = (t >= step).astype(float)
        recover = ov.get("recover", rng.random() < .35)
        if recover:
            H = rng.uniform(8, 20)
            z -= delta * after * np.exp(-np.clip(t - step, 0, None) / H)
        else:
            z -= delta * after
        a.override["step"] = step
        forced_leave = ov.get("champion_left")
        if forced_leave is True or (a.champion_left_week is None and forced_leave is None and rng.random() < .55):
            a.champion_left_week = int(step)
    elif arch == "recovering":
        low, high = rng.normal(-1.2, .4), rng.normal(1.0, .4)
        t0 = int(rng.integers(-20, 40))
        L = int(rng.integers(12, 30))
        z = low + (high - low) * np.clip((t - t0) / L, 0, 1) + ar1(rng, T_TOTAL, .8, .25)
    else:
        raise ValueError(arch)
    return sigmoid(z)


# --------------------------------------------------------------------------------
# Renewals / seat timeline
# --------------------------------------------------------------------------------
def simulate_lifecycle(rng, a: Account, t: TenantConfig) -> None:
    h = a.h
    seats = a.seats0
    a.seat_events = [SeatEvent(a.contract_start, seats, "start")]

    # mid-term seat changes (independent of renewal decisions)
    mid: list[SeatEvent] = []
    if a.archetype in ("expanding", "steady_healthy") and rng.random() < (.8 if a.archetype == "expanding" else .25):
        for _ in range(int(rng.poisson(2.0 if a.archetype == "expanding" else 1.0))):
            d = week_date(int(rng.integers(0, N_WEEKS - 4)))
            mid.append(SeatEvent(d, 0, "upgrade"))
    if a.archetype in ("slow_decline", "sudden_drop") or a.role.startswith("lost"):
        below = np.where(h[N_PRE:N_PRE + N_WEEKS] < .38)[0]
        if len(below) and rng.random() < (.4 if not a.role else .0):
            mid.append(SeatEvent(week_date(int(below[0])), 0, "downgrade_mid"))
    mid = [m for m in mid if m.day > a.contract_start]
    mid.sort(key=lambda e: e.day)

    horizon = EXPORT_DATE + timedelta(days=200)
    mi = 0
    for k, R in enumerate(a.renewal_dates, start=1):
        if R < WINDOW_START or R > horizon:
            continue
        # apply mid-term changes before R
        while mi < len(mid) and mid[mi].day < R:
            m = mid[mi]
            mi += 1
            if m.kind == "upgrade":
                seats = int(round(seats * rng.uniform(1.1, 1.25))) + 1
            else:
                seats = max(1, int(round(seats * rng.uniform(.6, .8))))
            a.seat_events.append(SeatEvent(m.day, seats, m.kind))
        jR = week_idx(R) + N_PRE
        lo, hi = max(jR - 17, 0), jR - 4
        hp = float(h[lo:hi].mean())
        logit = t.churn_alpha - CHURN_K * (hp - .5) + SEG_ADJ[a.segment] + (.3 if k == 1 else 0.0)
        p = float(sigmoid(logit))
        exo = bool(rng.random() < EXOGENOUS_CHURN)
        if R in a.forced:
            outcome, exo = a.forced[R], False
        elif a.role:
            outcome = "renewed"
        else:
            u = rng.random()
            if exo or u < p:
                outcome = "churned"
            elif rng.random() < float(sigmoid(-2.4 - 3.0 * (hp - .5))):
                outcome = "downgraded"
            elif hp > .7 and rng.random() < .18:
                outcome = "expanded"
            else:
                outcome = "renewed"
        notice = None
        seats_before = seats
        if outcome == "churned":
            notice = R - timedelta(days=int(rng.integers(7, 61)))
            notice = max(notice, a.contract_start + timedelta(days=30))
            seats_after = 0
        elif outcome == "downgraded":
            seats_after = max(1, int(round(seats * rng.uniform(.4, .75))))
        elif outcome == "expanded":
            seats_after = int(round(seats * rng.uniform(1.2, 1.6))) + 1
        else:
            seats_after = seats
        pending = R > EXPORT_DATE
        a.renewals.append(Renewal(
            k=k, day=R, hp=hp, p_churn=p, outcome="pending" if pending else outcome,
            hidden_outcome=outcome, exogenous=exo, notice_day=notice,
            seats_before=seats_before, seats_after=seats_after))
        if outcome == "churned":
            a.end_date = R
            break
        if outcome != "renewed" and not pending:
            kind = "renewal_expand" if outcome == "expanded" else "renewal_downgrade"
            a.seat_events.append(SeatEvent(R, seats_after, kind))
        seats = seats_after if not pending else seats
    a.seat_events.sort(key=lambda e: e.day)


# --------------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------------
TITLES = {
    "champion": ("Director of Operations", "Head of Analytics", "Operations Manager", "Product Owner",
                 "Team Lead", "VP Operations"),
    "decision_maker": ("CFO", "COO", "VP Finance", "CEO", "Head of Procurement", "General Manager"),
    "user": ("Analyst", "Coordinator", "Associate", "Specialist", "Administrator", "Manager"),
}


def make_contacts(rng, a: Account, names: NameFactory) -> None:
    n = 1 + int(min(5, round(a.seats0 ** .3)))
    roles = ["champion"] + (["decision_maker"] if n > 1 else []) + ["user"] * max(0, n - 2)
    for role in roles:
        f, l = names.person()
        personal = rng.random() < .10 or (a.segment == "smb" and rng.random() < .15)
        dom = str(rng.choice(PERSONAL)) if personal else a.domain
        if not personal and a.alias_domain and rng.random() < .4:
            dom = a.alias_domain
        email = person_email(rng, f, l, dom)
        a.contacts.append(Contact(
            contact_id=rid(rng, "003", 15), first=f, last=l, email=email,
            title=str(rng.choice(TITLES[role])), role=role,
            sf_champion_flag=(role == "champion" and rng.random() < .6),
            personal_email=personal))
    # champion departures (also happens on healthy accounts: not every departure means churn)
    champ = a.contacts[0]
    if a.champion_left_week is None and rng.random() < .03:
        a.champion_left_week = int(rng.integers(10, N_WEEKS - 5))
    if a.champion_left_week is not None:
        champ.left_week = a.champion_left_week


# --------------------------------------------------------------------------------
# Tenant builder
# --------------------------------------------------------------------------------
def build_tenant(t: TenantConfig, rng, names: NameFactory, account_scale: float = 1.0) -> list[Account]:
    n = max(20, int(t.n_accounts * account_scale))
    seg_names = ("smb", "mid", "ent")
    arch_names = [a for a, _ in ARCHETYPES]
    arch_p = np.array([p for _, p in ARCHETYPES])
    accs: list[Account] = []
    window_days = (EXPORT_DATE - WINDOW_START).days

    for i in range(n):
        seg = seg_names[int(rng.choice(3, p=t.segment_mix))]
        if seg == "smb":
            seats = int(np.clip(rng.lognormal(1.9, .55), 2, 40))
            price = float(rng.choice([29, 39, 59], p=[.5, .3, .2]))
            disc = rng.uniform(.9, 1.0)
        elif seg == "mid":
            seats = int(np.clip(rng.lognormal(4.0, .5), 20, 300))
            price = float(rng.choice([49, 59, 79], p=[.3, .5, .2]))
            disc = rng.uniform(.75, 1.0)
        else:
            seats = int(np.clip(rng.lognormal(5.9, .5), 150, 3000))
            price = float(rng.choice([69, 89, 99], p=[.3, .4, .3]))
            disc = rng.uniform(.75, 1.0)
        base, domain = names.company()
        g = rng.random()
        if g < .28:                                            # started before the window
            start = date(2021, 1, 1) + timedelta(days=int(rng.integers(0, (WINDOW_START - date(2021, 1, 1)).days)))
        elif g < .96:                                          # growth: denser later in the window
            start = WINDOW_START + timedelta(days=int(window_days * rng.random() ** (1 / 1.4)))
        else:                                                  # cold start: last ~100 days
            start = EXPORT_DATE - timedelta(days=int(rng.integers(5, 100)))
        arch = str(rng.choice(arch_names, p=arch_p))
        missing = set()
        if rng.random() < {"smb": .07, "mid": .03, "ent": .03}[seg]:
            missing.add("product")
        if rng.random() < {"smb": .06, "mid": .10, "ent": .18}[seg]:
            missing.add("zendesk")
        a = Account(
            tenant_id=t.tenant_id, idx=i, true_id=f"{t.prefix}-{i + 1:05d}", base_name=base,
            legal=legal_name(rng, base), domain=domain,
            alias_domain=None, industry=str(rng.choice(INDUSTRIES)), segment=seg,
            employees=int(seats * rng.uniform(2, 6)), tz=float(rng.choice(t.tz_offsets)),
            seats0=seats, unit_price=round(price * disc, 2),
            billing="monthly" if (rng.random() < (t.monthly_share if seg == "smb" else .03)) else "annual",
            contract_start=start, archetype=arch,
            act_mult=(.25 if arch == "quiet_loyal" else 1.0) * float(rng.lognormal(0, .35)),
            missing_sources=missing)
        if rng.random() < MESS["alias_domain"]:
            a.alias_domain = names._domain(base + " group")
        # renewal dates: anniversaries within [window start, cutoff + 200d]
        k = 1
        while True:
            r = add_years(start, k)
            if r > EXPORT_DATE + timedelta(days=200):
                break
            if r >= WINDOW_START:
                a.renewal_dates.append(r)
            k += 1
        accs.append(a)

    # name twins: different companies with near-identical names
    for j in rng.choice(n, size=int(MESS["name_twin"] * n), replace=False):
        src = accs[int(rng.integers(0, n))]
        if src is accs[j]:
            continue
        base, dom = names.twin(src.base_name)
        accs[j].base_name, accs[j].domain = base, dom
        accs[j].legal = legal_name(rng, base)
        accs[j].twin_of = src.true_id

    if t.tenant_id == "tnt_lumen":
        _plant_cases(rng, accs)
    return accs


# --------------------------------------------------------------------------------
# Planted cases (tnt_lumen): the "two renewals nobody saw coming" + two hard negatives
# --------------------------------------------------------------------------------
PLANTED = (
    dict(role="lost_A", renewal=date(2026, 8, 14), archetype="slow_decline", outcome="churned",
         override=dict(lead=28, L=22, drop=3.6, base=1.6), seats=96,
         mess=("no_zd_org", "no_stripe_sf_link"),
         note="Slow burn. Usage falls ~50% over ~10 weeks; bug/performance tickets rise; one "
              "cancellation-intent ticket. Zendesk tickets are NOT linked to a Zendesk organisation and "
              "Stripe has no Salesforce id: a naive join misses every signal."),
    dict(role="lost_B", renewal=date(2026, 9, 9), archetype="sudden_drop", outcome="churned",
         override=dict(delta=3.3, base=1.5, recover=False, champion_left=True, lead=20), seats=118,
         mess=("workspace_name_mismatch",),
         note="Champion leaves ~20 weeks before renewal. Emails to the champion bounce, usage collapses "
              "after the departure. Product workspace is named differently from the CRM account."),
    dict(role="decoy_declining_renewed", renewal=date(2026, 8, 27), archetype="slow_decline", outcome="renewed",
         override=dict(lead=24, L=16, drop=2.2, base=1.3), seats=84, mess=(),
         note="Looks like lost_A for a while, then the CSM saves it. A hard negative: a system that "
              "flags every decline will be right here only by luck."),
    dict(role="decoy_quiet_renewed", renewal=date(2026, 9, 18), archetype="quiet_loyal", outcome="renewed",
         override=dict(), seats=72, mess=(),
         note="Very little data in every source, renews anyway. 'Quiet' is not 'unhealthy'."),
)


def _plant_cases(rng, accs: list[Account]) -> None:
    mids = [a for a in accs if a.segment == "mid" and not a.twin_of]
    picks = rng.choice(len(mids), size=len(PLANTED), replace=False)
    for p, pi in zip(PLANTED, picks):
        a = mids[int(pi)]
        a.role = p["role"]
        a.archetype = p["archetype"]
        a.seats0 = p["seats"]
        a.segment = "mid"
        a.billing = "annual"
        a.act_mult = .25 if p["archetype"] == "quiet_loyal" else 3.0   # engaged accounts: decline is visible
        a.missing_sources = set()
        a.contract_start = add_years(p["renewal"], -3)          # renewals in 2024, 2025, 2026
        a.renewal_dates = [add_years(a.contract_start, k) for k in (1, 2, 3, 4)]
        a.override = dict(p["override"], anchor=week_idx(p["renewal"]))
        a.forced = {p["renewal"]: p["outcome"]}
        a.alias_domain = None
        a.planted_mess = p["mess"]
        a.planted_note = p["note"]
        a.planted_renewal = p["renewal"]


# --------------------------------------------------------------------------------
# World assembly
# --------------------------------------------------------------------------------
def finish_accounts(t: TenantConfig, accs: list[Account], rng, names: NameFactory):
    """Health, lifecycle, contacts, CSM assignment. Returns the CSM user list."""
    for a in accs:
        make_contacts(rng, a, names)           # (may set champion_left_week)
        a.h = simulate_health(rng, a)          # sudden_drop may also set champion_left_week
        if a.champion_left_week is not None and a.contacts:
            a.contacts[0].left_week = a.champion_left_week
        simulate_lifecycle(rng, a, t)

    active = [a for a in accs if not a.churned]
    n_active_csm = max(3, int(round(len(active) / 120)))          # ~120 live accounts per CSM
    n_departed = max(1, int(round(.09 * n_active_csm)))           # ex-employees still in the CRM
    csms = []
    for i in range(n_active_csm + n_departed):
        f, l = names.person()
        csms.append(dict(
            user_id=rid(rng, "005", 15), name=f"{f} {l}", email=f"{f.lower()}.{l.lower()}@{t.csm_domain}",
            active=i < n_active_csm))
    active_ids = [c["user_id"] for c in csms if c["active"]]
    all_ids = [c["user_id"] for c in csms]
    order = rng.permutation(len(active))
    for pos, ai in enumerate(order):
        a = active[int(ai)]
        a.csm_id = active_ids[pos % len(active_ids)]
        if rng.random() < MESS["sf_inactive_owner"]:       # stale ownership
            inactive = [c["user_id"] for c in csms if not c["active"]]
            if inactive:
                a.csm_id = str(rng.choice(inactive))
    for a in accs:
        if a.churned:
            a.csm_id = str(rng.choice(all_ids))
        if rng.random() < .06 and len(all_ids) > 1:
            a.prev_csm_id = str(rng.choice([c for c in all_ids if c != a.csm_id]))
            a.reassign_week = int(rng.integers(20, N_WEEKS - 10))
    return csms
