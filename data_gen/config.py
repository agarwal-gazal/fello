"""Central configuration: time window, tenants, and calibration constants.

Everything that shapes the data lives here so the walkthrough can point at one file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

# --- Time ---------------------------------------------------------------------
WINDOW_START = date(2023, 10, 30)               # a Monday; ~3 years of history
N_WEEKS = 157                                   # weeks 0..156; week 156 is partial
EXPORT_AT = datetime(2026, 10, 27, tzinfo=timezone.utc)   # data cutoff ("now")
EXPORT_DATE = EXPORT_AT.date()
N_PRE = 26                                      # latent weeks simulated before the window
N_FUT = 40                                      # latent weeks simulated after the cutoff
T_TOTAL = N_PRE + N_WEEKS + N_FUT               # (future is only used to decide hidden
                                                #  outcomes of upcoming renewals)


@dataclass(frozen=True)
class TenantConfig:
    """One customer organisation of the platform (each runs its own SaaS business)."""
    tenant_id: str
    name: str
    prefix: str
    n_accounts: int
    csm_domain: str
    churn_alpha: float          # intercept of the renewal-churn logit (higher = churnier)
    segment_mix: tuple          # (smb, mid, enterprise)
    monthly_share: float        # share of SMB accounts billed monthly
    stripe_sf_link: float       # share of Stripe customers carrying a Salesforce id
    zd_org_rate: float          # share of accounts that have a Zendesk organisation
    ticket_mult: float
    usage_mult: float
    email_mult: float
    gmail_connected: float      # share of CSM mailboxes connected to the warehouse
    tz_offsets: tuple           # customer timezones (hours from UTC)
    features: tuple             # (event_name, weight, is_advanced)


TENANTS = (
    TenantConfig(
        tenant_id="tnt_lumen", name="Lumen Analytics", prefix="lum", n_accounts=2300,
        csm_domain="lumenanalytics.com", churn_alpha=-1.75, segment_mix=(0.52, 0.38, 0.10),
        monthly_share=0.15, stripe_sf_link=0.62, zd_org_rate=0.80, ticket_mult=1.0,
        usage_mult=1.0, email_mult=1.0, gmail_connected=0.92,
        tz_offsets=(-8, -5, -5, 0, 1, 5.5),
        features=(("dashboard_viewed", .30, False), ("query_run", .22, False),
                  ("report_created", .12, False), ("export_csv", .08, False),
                  ("share_link_created", .06, True), ("integration_connected", .04, True),
                  ("api_call", .12, True), ("alert_configured", .06, True)),
    ),
    TenantConfig(
        tenant_id="tnt_parcel", name="Parcelwise Logistics Cloud", prefix="par", n_accounts=1700,
        csm_domain="parcelwise.io", churn_alpha=-1.40, segment_mix=(0.68, 0.27, 0.05),
        monthly_share=0.22, stripe_sf_link=0.38, zd_org_rate=0.65, ticket_mult=1.3,
        usage_mult=1.2, email_mult=0.8, gmail_connected=0.80,
        tz_offsets=(-6, -5, 0, 1, 2, 8),
        features=(("shipment_created", .32, False), ("label_printed", .20, False),
                  ("tracking_viewed", .18, False), ("route_optimized", .10, True),
                  ("invoice_exported", .06, False), ("api_call", .10, True),
                  ("carrier_added", .04, True)),
    ),
    TenantConfig(
        tenant_id="tnt_beacon", name="Beacon HR", prefix="bea", n_accounts=1000,
        csm_domain="beaconhr.com", churn_alpha=-2.00, segment_mix=(0.40, 0.45, 0.15),
        monthly_share=0.08, stripe_sf_link=0.78, zd_org_rate=0.90, ticket_mult=0.8,
        usage_mult=0.9, email_mult=1.3, gmail_connected=0.97,
        tz_offsets=(-8, -6, -5, 0, 5.5),
        features=(("timesheet_submitted", .30, False), ("employee_added", .12, False),
                  ("leave_approved", .20, False), ("payroll_run", .10, False),
                  ("report_run", .12, True), ("onboarding_completed", .08, True),
                  ("integration_synced", .08, True)),
    ),
)

# --- Hidden-health archetypes (probabilities sum to 1) ---------------------------
ARCHETYPES = (
    ("steady_healthy", .36), ("steady_ok", .17), ("slow_decline", .12),
    ("sudden_drop", .09), ("recovering", .06), ("volatile", .07),
    ("quiet_loyal", .08), ("expanding", .05),
)

# --- Renewal decision model (ground truth, NOT the model we will fit) --------------
CHURN_K = 9.0               # sensitivity of churn logit to pre-renewal health
SEG_ADJ = {"smb": 0.5, "mid": 0.0, "ent": -0.6}
EXOGENOUS_CHURN = 0.02      # acquired / budget cut: happens regardless of health

# --- Volume calibration (tuned so the full run lands near ~200k events) -----------
USAGE_C = 0.0227            # product events per (seats**USAGE_EXP) per week at full health
USAGE_EXP = 0.75            # large accounts generate disproportionately more events
TICKET_C = 0.0133
EMAIL_OUT = {"smb": 0.0083, "mid": 0.0276, "ent": 0.083}   # CSM-initiated threads / week
EMAIL_IN = {"smb": 0.0053, "mid": 0.0179, "ent": 0.0538}   # customer-initiated threads / week

# --- Mess rates ----------------------------------------------------------------
MESS = dict(
    sf_dup=0.03, name_twin=0.006, alias_domain=0.05, website_missing=0.08,
    sf_stale_arr=0.25, sf_stale_end=0.08, sf_inactive_owner=0.03,
    zd_ticket_unlinked=0.20, zd_dup_version=0.03, zd_personal_email=0.12,
    stripe_test_customers=0.01, stripe_dup_webhook=0.025, stripe_late=0.03,
    prod_dup=0.015, prod_naive_ts=0.04, prod_late=0.06, prod_backfill=0.02,
    prod_future_ts=0.003, prod_null_ws=0.01, prod_internal=0.02,
    gmail_noise=0.08, gmail_never_connected=0.06,
)
