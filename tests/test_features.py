"""Point-in-time feature tests.  python -m unittest tests.test_features -v"""
from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

from core import build_core
from features import features
from features.build_features import TABLES, load_tables, training_snapshots
from resolve import resolve as er
from tests.helpers import DB, build_dataset, cleanup, load_and_stage


def mini_tables(email_known_late_days=0):
    """One account, hand-made, so every expected number can be checked by eye."""
    T = lambda s: pd.Timestamp(s, tz="UTC")
    acct = {"tenant_id": "t", "account_id": "a"}
    email = pd.DataFrame([
        {**acct, "sent_at": T("2026-01-10"), "known_at": T("2026-01-10") + pd.Timedelta(days=email_known_late_days), "direction": "inbound",
         "is_bounce": "f", "bounced_address": None, "is_auto_reply": "f"},
        {**acct, "sent_at": T("2026-01-12"), "known_at": T("2026-01-12"), "direction": "outbound", "is_bounce": "f",
         "bounced_address": None, "is_auto_reply": "f"}])
    prod = pd.DataFrame([{**acct, "event_name": "login", "user_id": "u1", "event_ts": T("2026-01-20"), "known_at": T("2026-01-20"), "api_status": None}])
    empty = lambda cols: pd.DataFrame(columns=["tenant_id", "account_id"] + cols)
    return {
        "product_event": prod, "email": email,
        "ticket": empty(["created_at", "known_at", "solved_at", "priority", "category", "is_reopened", "first_reply_minutes", "csat"]),
        "billing_event": empty(["event_at", "known_at", "event_type", "quantity", "previous_quantity"]),
        "invoice": empty(["created_at", "known_at", "status"]),
        "account": pd.DataFrame([{**acct, "segment": "smb", "employees": 10, "created_at": T("2025-01-01")}]),
        "contact": pd.DataFrame([{**acct, "email": "boss@x.com", "is_champion": "t"}]),
    }


def snap(at):
    return pd.DataFrame([{"tenant_id": "t", "account_id": "a", "snap_at": pd.Timestamp(at, tz="UTC"), "renewal_date": "2026-06-01"}])


class HandMadeTests(unittest.TestCase):
    def test_event_after_snapshot_is_invisible(self):
        f = features.compute(snap("2026-01-15"), mini_tables()).iloc[0]       # product login happens Jan 20
        self.assertEqual(0, f.p_28)
        self.assertEqual(0, f.has_product)
        f = features.compute(snap("2026-01-25"), mini_tables()).iloc[0]
        self.assertEqual(1, f.p_28)

    def test_backfilled_email_is_invisible_until_it_arrives(self):
        late = mini_tables(email_known_late_days=40)         # sent Jan 10, reached the warehouse Feb 19
        before = features.compute(snap("2026-02-01"), late).iloc[0]
        after = features.compute(snap("2026-02-25"), late).iloc[0]
        self.assertEqual(0, before.em_in_30 + before.em_in_90, "an email the warehouse had not received must not count")
        self.assertEqual(1, after.em_in_90)
        # the same email, delivered on time, IS visible at the earlier snapshot: only known_at differs
        ontime = features.compute(snap("2026-02-01"), mini_tables()).iloc[0]
        self.assertEqual(1, ontime.em_in_90)

    def test_windows_are_half_open(self):
        f = features.compute(snap("2026-02-09"), mini_tables()).iloc[0]       # Jan 10 is exactly 30 days before: inside (0, 30]
        self.assertEqual(1, f.em_in_30)
        f = features.compute(snap("2026-02-10"), mini_tables()).iloc[0]
        self.assertEqual(0, f.em_in_30)
        self.assertEqual(1, f.em_in_90)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.data = build_dataset(scale=0.08, seed=5)
        load_and_stage(cls.data)
        er.run(DB)
        m = json.loads((cls.data / "manifest.json").read_text())
        build_core.build(DB, m["export_at"])
        cls.window_start = m["window_start"]
        cls.tables = load_tables(DB)

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def test_hiding_everything_that_arrives_later_changes_nothing(self):
        """The leak test. Take snapshots at several dates; recompute each from tables that physically lack every row
        that was not yet known at that date. Identical features mean nothing in the future was used."""
        snaps = training_snapshots(DB, self.window_start)
        snaps = snaps[snaps.account_id.isin(snaps.account_id.drop_duplicates().sample(80, random_state=1))].head(400)
        full = features.compute(snaps, self.tables).sort_values(["tenant_id", "account_id", "snap_at", "renewal_date"]).reset_index(drop=True)
        parts = []
        for at, grp in snaps.groupby("snap_at"):
            cut = pd.Timestamp(at, tz="UTC") if pd.Timestamp(at).tzinfo is None else pd.Timestamp(at)
            hidden = {}
            for name, df in self.tables.items():
                if "known_at" in df.columns:
                    ka = pd.to_datetime(df["known_at"], utc=True, format="ISO8601")
                    df = df[ka <= cut]
                hidden[name] = df
            parts.append(features.compute(grp, hidden))
        hid = pd.concat(parts).sort_values(["tenant_id", "account_id", "snap_at", "renewal_date"]).reset_index(drop=True)
        cols = [c for c in full.columns if c not in ("outcome",)]
        pd.testing.assert_frame_equal(full[cols], hid[cols], check_exact=False, rtol=1e-9)

    def test_no_snapshot_after_the_label_horizon_or_before_the_account_existed(self):
        snaps = training_snapshots(DB, self.window_start)
        self.assertTrue((snaps.snap_at < snaps.renewal_date + pd.Timedelta(hours=1)).all())
        self.assertTrue((snaps.snap_at >= pd.Timestamp(self.window_start) + pd.Timedelta(days=28)).all())
        self.assertFalse(snaps.outcome.eq("pending").any())

    def test_training_features_contain_no_label_derived_columns(self):
        f = features.compute(training_snapshots(DB, self.window_start).head(50), self.tables)
        banned = {"cancel_at_period_end", "crm_stage", "arr", "contract_end", "stage"}
        self.assertFalse([c for c in f.columns if any(b in c for b in banned)])


if __name__ == "__main__":
    unittest.main()
