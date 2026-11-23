"""Monday agent tests.  python -m unittest tests.test_agent -v"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import pickle
import re
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from agent import diff, drafts, run as agent_run, scheduler
from agent.db import psql
from core import build_core
from features import build_features
from model import train
from resolve import resolve as er
from resolve.io import read_df
from tests.helpers import DB, build_dataset, cleanup, load_and_stage, q

MONDAYS = ["2026-09-21", "2026-09-28", "2026-10-05"]
NUM = re.compile(r"\d+")


def tokens(text: str) -> set:
    return set(NUM.findall(text))


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        psql(DB, "drop schema if exists agent cascade; drop schema if exists score cascade")
        cls.tmp, cls.data = build_dataset(scale=0.1, seed=5)
        load_and_stage(cls.data)
        er.run(DB)
        m = json.loads((cls.data / "manifest.json").read_text())
        build_core.build(DB, m["export_at"])
        build_features.main(["--database-url", DB, "--data", str(cls.data)])
        pipe, cols = train.fit_final(train.load(DB), pd.Timestamp("2026-06-01"))
        cls.model_path = str(cls.tmp / "model.pkl")
        Path(cls.model_path).write_bytes(pickle.dumps({"model": pipe, "columns": cols, "trained_through": "2026-06-01", "metrics": {}}))
        cls.results = [agent_run.execute(DB, pd.Timestamp(d), cls.model_path) for d in MONDAYS]

    @classmethod
    def tearDownClass(cls):
        psql(DB, "drop schema if exists agent cascade; drop schema if exists score cascade")
        cleanup(cls.tmp)

    def drafts(self, where="true") -> pd.DataFrame:
        d = read_df(DB, f"select draft_id, run_id, tenant_id, account_id, kind, to_address, subject, body, status, decided_by, reasons::text reasons, created_at::text created_at from agent.draft where {where}")
        return d

    # ------------------------------------------------------------------------------------------------------ runs and diffs
    def test_runs_are_recorded_with_counts_that_match_the_tables(self):
        for d in MONDAYS:
            rid = f"agent-{d.replace('-', '')}"
            row = q(f"select status, n_scored, n_changes, n_drafts from agent.run where run_id = '{rid}'").split("|")
            self.assertEqual("ok", row[0])
            self.assertGreater(int(row[1]), 20)
            self.assertEqual(row[2], q(f"select count(*) from agent.change where run_id = '{rid}'"))
            self.assertEqual(row[3], q(f"select count(*) from agent.draft where run_id = '{rid}'"))
            self.assertEqual("1", q(f"select count(*) from score.run where run_id = (select score_run_id from agent.run where run_id = '{rid}')"))

    def test_first_run_is_a_baseline_with_no_changes(self):
        self.assertEqual("0", q("select count(*) from agent.change where run_id = 'agent-20260921'"))
        self.assertGreater(int(q("select count(*) from agent.draft where run_id = 'agent-20260921'")), 0, "the baseline still queues drafts for what is red now")

    def test_changes_match_an_independent_computation(self):
        def band_map(rid):
            df = read_df(DB, f"select tenant_id, account_id, band, risk from score.account_score where run_id = (select score_run_id from agent.run where run_id = '{rid}')")
            return {(r.tenant_id, r.account_id): (r.band, float(r.risk)) for r in df.itertuples()}
        for old, new in (("agent-20260921", "agent-20260928"), ("agent-20260928", "agent-20261005")):
            a, b = band_map(old), band_map(new)
            entered = {k for k, (bd, _) in b.items() if bd == "red" and a.get(k, (None,))[0] != "red"}
            left = {k for k, (bd, _) in a.items() if bd == "red" and k in b and b[k][0] != "red"}
            got = read_df(DB, f"select tenant_id, account_id, kind from agent.change where run_id = '{new}'")
            self.assertEqual(entered, set(zip(got[got.kind == "entered_red"].tenant_id, got[got.kind == "entered_red"].account_id)))
            self.assertEqual(left, set(zip(got[got.kind == "left_red"].tenant_id, got[got.kind == "left_red"].account_id)))
            self.assertTrue(entered or left, "fixture should contain at least one transition between these Mondays")

    def test_diff_unit_cases(self):
        row = lambda **k: {**dict(tenant_id="t", account_id="a", band="green", risk=.1, renewal_date="2026-12-01", weeks_to_renewal=8.0,
                                  alerts=[], csm_active=True, csm_name="X"), **k}
        prev = pd.DataFrame([row()])
        self.assertEqual(["entered_red"], list(diff.compute(prev, pd.DataFrame([row(band="red", risk=.35)])).kind))
        self.assertEqual(["risk_jump"], list(diff.compute(prev, pd.DataFrame([row(risk=.25)])).kind))
        self.assertEqual([], list(diff.compute(prev, pd.DataFrame([row(risk=.25)]), same_model=False).kind), "a new model makes risks incomparable")
        self.assertEqual(["new_alert"], list(diff.compute(prev, pd.DataFrame([row(alerts=[{"kind": "cancel_request"}])])).kind))
        self.assertEqual(["csm_departed"], list(diff.compute(prev, pd.DataFrame([row(csm_active=False)])).kind))
        self.assertEqual(["left_red"], list(diff.compute(pd.DataFrame([row(band="red", risk=.4)]), pd.DataFrame([row()])).kind))
        self.assertEqual([], list(diff.compute(None, pd.DataFrame([row(band="red")])).kind))

    # ------------------------------------------------------------------------------------------------------ drafts
    def test_every_number_and_date_in_a_draft_comes_from_its_stored_reasons(self):
        d = self.drafts()
        self.assertGreater(len(d), 10)
        for r in d.itertuples():
            allowed = tokens(r.reasons)
            extra = tokens(r.subject + "\n" + r.body) - allowed
            self.assertFalse(extra, f"draft {r.draft_id} ({r.kind}) cites numbers that are not in its reasons: {extra}")

    def test_driver_and_alert_sentences_are_quoted_verbatim_from_the_score_row(self):
        for r in self.drafts("kind = 'csm_note'").head(40).itertuples():
            reasons = json.loads(r.reasons)
            for line in reasons["drivers"] + reasons["alerts"]:
                self.assertIn(line, r.body)
            stored = json.loads(q(f"select json_build_object('d', drivers, 'a', alerts) from score.account_score where run_id = '{reasons['score_run']}' "
                                  f"and tenant_id = '{r.tenant_id}' and account_id = '{r.account_id}'"))
            self.assertEqual([x["text"] for x in stored["d"]], reasons["drivers"])
            self.assertEqual([x["text"] for x in stored["a"]], reasons["alerts"])

    def test_customer_emails_are_for_red_accounts_with_an_active_csm_and_never_expose_scores(self):
        mails = self.drafts("kind = 'customer_email'")
        self.assertGreater(len(mails), 3)
        for r in mails.itertuples():
            self.assertFalse(drafts.BANNED_IN_CUSTOMER_EMAIL.search(r.subject + r.body), f"internal language in customer email {r.draft_id}")
            reasons = json.loads(r.reasons)
            row = q(f"select band, csm_active from score.account_score where run_id = '{reasons['score_run']}' and tenant_id = '{r.tenant_id}' and account_id = '{r.account_id}'")
            self.assertEqual("red|t", row)
            self.assertTrue(r.to_address and "@" in r.to_address)

    def test_customer_emails_never_go_to_a_bounced_address_and_come_from_the_accounts_contacts(self):
        for r in self.drafts("kind = 'customer_email'").itertuples():
            self.assertEqual("1", q(f"select count(*) from core.contact where tenant_id = '{r.tenant_id}' and account_id = '{r.account_id}' and email = '{r.to_address}'"))
            self.assertEqual("0", q(f"select count(*) from core.email where tenant_id = '{r.tenant_id}' and account_id = '{r.account_id}' and is_bounce "
                                    f"and lower(bounced_address) = lower('{r.to_address}')"))

    def test_accounts_whose_csm_left_get_a_routing_note_and_no_customer_email(self):
        gone = read_df(DB, "select tenant_id, account_id, band from score.account_score where not csm_active and band in ('red', 'yellow')")
        self.assertGreater(len(gone), 0, "fixture should contain at-risk accounts with a departed CSM")
        keys = set(zip(gone.tenant_id, gone.account_id))
        d = self.drafts()
        notes = d[(d.kind == "csm_note") & np.array([k in keys for k in zip(d.tenant_id, d.account_id)])]
        self.assertGreater(len(notes), 0)
        self.assertTrue(notes.body.str.contains("has no active owner").all())
        mails = d[(d.kind == "customer_email") & np.array([k in keys for k in zip(d.tenant_id, d.account_id)])]
        self.assertEqual(0, len(mails))

    def test_agent_never_sends_or_approves_anything(self):
        d = self.drafts()
        self.assertTrue(d.status.isin(["pending", "rejected"]).all())
        self.assertTrue((d[d.status == "rejected"].decided_by == "agent:superseded").all())
        self.assertTrue(d[d.status == "pending"].decided_by.isna().all())

    def test_dedupe_inside_the_window_and_supersede_after_it(self):
        pend = self.drafts("status = 'pending'")
        self.assertFalse(pend.duplicated(["tenant_id", "account_id", "kind"]).any(), "at most one open draft per account and kind")
        # 09-21 drafts are 7 days old on 09-28: nothing re-drafted for them. On 10-05 they are exactly 14 days old: refreshed.
        first = self.drafts("run_id = 'agent-20260921'")
        second = self.drafts("run_id = 'agent-20260928'")
        k1, k2 = set(zip(first.tenant_id, first.account_id, first.kind)), set(zip(second.tenant_id, second.account_id, second.kind))
        self.assertFalse(k1 & k2, "a draft written a week ago blocks a new one")
        third = self.drafts("run_id = 'agent-20261005'")
        k3 = set(zip(third.tenant_id, third.account_id, third.kind))
        self.assertTrue(k1 & k3, "fixture should have an account that stays red for 2+ weeks")
        old = first[[k in k3 for k in zip(first.tenant_id, first.account_id, first.kind)]]
        self.assertTrue((old.status == "rejected").all() and (old.decided_by == "agent:superseded").all())

    def test_generate_skips_keys_that_already_have_a_recent_draft(self):
        stored = agent_run.load_stored(DB, q("select score_run_id from agent.run where run_id = 'agent-20261005'"))
        asof = pd.Timestamp("2026-10-05")
        contacts, bounced = agent_run.contact_maps(DB, asof)
        every = drafts.generate(stored, pd.DataFrame(columns=diff.COLS), contacts, bounced, set(), asof)
        blocked = {(every.iloc[0].tenant_id, every.iloc[0].account_id, every.iloc[0].kind)}
        fewer = drafts.generate(stored, pd.DataFrame(columns=diff.COLS), contacts, bounced, blocked, asof)
        self.assertEqual(len(every) - 1, len(fewer))

    # ------------------------------------------------------------------------------------------------------ safety
    def test_rerun_is_idempotent_and_never_touches_decided_drafts(self):
        victim = q("select draft_id from agent.draft where run_id = 'agent-20260928' and status = 'pending' order by draft_id limit 1")
        q(f"update agent.draft set status = 'approved', decided_by = 'gazal', decided_at = now(), edited_body = 'my edit' where draft_id = {victim}")
        before = (q("select count(*) from agent.draft"), q("select count(*) from agent.change"), q("select count(*) from agent.run"))
        agent_run.execute(DB, pd.Timestamp("2026-09-28"), self.model_path)
        agent_run.execute(DB, pd.Timestamp("2026-09-28"), self.model_path)
        self.assertEqual(before, (q("select count(*) from agent.draft"), q("select count(*) from agent.change"), q("select count(*) from agent.run")))
        self.assertEqual("approved|gazal|my edit", q(f"select status || '|' || decided_by || '|' || edited_body from agent.draft where draft_id = {victim}"))

    def test_failure_is_recorded_loudly_and_a_later_rerun_repairs_it(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = agent_run.main(["--database-url", DB, "--asof", "2026-10-12", "--model", "/nonexistent/model.pkl"])
        self.assertEqual(1, rc)
        self.assertIn("FAILED", err.getvalue())
        status, msg = q("select status, error from agent.run where run_id = 'agent-20261012'").split("|", 1)
        self.assertEqual("failed", status)
        self.assertTrue(msg.strip())
        self.assertEqual(0, agent_run.main(["--database-url", DB, "--asof", "2026-10-12", "--model", self.model_path]))
        self.assertEqual("ok", q("select status from agent.run where run_id = 'agent-20261012'"))
        self.assertEqual("", q("select coalesce(error, '') from agent.run where run_id = 'agent-20261012'"))

    def test_a_write_that_fails_halfway_leaves_nothing_behind(self):
        changes = pd.DataFrame([dict(tenant_id="t", account_id="a", kind="entered_red", from_band=None, to_band="red", from_risk=None, to_risk=.5, detail={})])
        bad = pd.DataFrame([dict(tenant_id="t", account_id="a", owner_id="o", kind="NOT_A_KIND", to_address=None, subject="s", body="b",
                                 reasons={}, created_at="2026-11-30 06:00:00+00")])
        with self.assertRaises(RuntimeError):
            agent_run.write_all(DB, "agent-20261130", pd.Timestamp("2026-11-30"), "sr", 1, "2026-11-30 06:00:00+00", changes, bad)
        self.assertEqual("0", q("select count(*) from agent.run where run_id = 'agent-20261130'"))
        self.assertEqual("0", q("select count(*) from agent.change where run_id = 'agent-20261130'"))

    def test_a_failed_run_publishes_no_scores(self):
        """The UI shows the latest score.run, so the agent's scores must commit together with its drafts, or not at all."""
        bad = pd.DataFrame([dict(tenant_id="t", account_id="a", owner_id="o", kind="NOT_A_KIND", to_address=None, subject="s", body="b",
                                 reasons={}, created_at="2026-09-14 06:00:00+00")])
        with mock.patch("agent.drafts.generate", return_value=bad), self.assertRaises(RuntimeError):
            agent_run.execute(DB, pd.Timestamp("2026-09-14"), self.model_path)
        self.assertEqual("0", q("select count(*) from score.run where asof = '2026-09-14 06:00+00'"))
        self.assertEqual("0", q("select count(*) from score.account_score where run_id like '20260914-%'"))
        self.assertEqual("0", q("select count(*) from agent.run where run_id = 'agent-20260914'"))

    def test_stored_rows_in_memory_match_what_the_database_returns(self):
        sid = q("select score_run_id from agent.run where run_id = 'agent-20261005'")
        from_db = agent_run.load_stored(DB, sid).sort_values(["tenant_id", "account_id"]).reset_index(drop=True)
        model = pickle.loads(Path(self.model_path).read_bytes())
        model["sha"] = sid.split("-", 1)[1]
        asof = pd.Timestamp("2026-10-05")
        from scoring import score as sc
        from features import features
        scored = sc.score(sc.population(DB, asof), asof, model, features.prepare(build_features.load_tables(DB)))
        mem = agent_run.stored_from_rows(sc.store_rows(sid, scored)).sort_values(["tenant_id", "account_id"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(from_db[agent_run.STORED_COLS], mem[agent_run.STORED_COLS])

    # ------------------------------------------------------------------------------------------------------ scheduler
    def test_latest_monday(self):
        f = lambda s: str(scheduler.latest_monday(pd.Timestamp(s)).date())
        self.assertEqual("2026-10-26", f("2026-10-26 07:00"))
        self.assertEqual("2026-10-19", f("2026-10-26 05:59"), "before Monday's run hour the previous Monday is the latest")
        self.assertEqual("2026-10-26", f("2026-10-30 12:00"))
        self.assertEqual("2026-10-26", f("2026-11-01 23:59"))
        self.assertEqual("2026-10-26", f("2026-10-27T00:00:00+00:00"))

    def test_scheduler_runs_once_then_does_nothing_and_respects_its_lock(self):
        args = ["--once", "--database-url", DB, "--now", "2026-10-19T09:00:00", "--model", self.model_path]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, scheduler.main(args))
        self.assertEqual("ok", q("select status from agent.run where run_id = 'agent-20261019'"))
        started = q("select started_at from agent.run where run_id = 'agent-20261019'")
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            self.assertEqual(0, scheduler.main(args))
        self.assertIn("already done", out2.getvalue())
        self.assertEqual(started, q("select started_at from agent.run where run_id = 'agent-20261019'"), "second call must not rerun")
        # a held lock means 'another run is in progress': exit 0, nothing written
        old_lock, scheduler.LOCK = scheduler.LOCK, str(self.tmp / "lock")
        try:
            with open(scheduler.LOCK, "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                out3 = io.StringIO()
                with contextlib.redirect_stdout(out3):
                    self.assertEqual(0, scheduler.main(["--once", "--database-url", DB, "--now", "2026-11-02T09:00:00", "--model", self.model_path]))
                self.assertIn("in progress", out3.getvalue())
                self.assertEqual("0", q("select count(*) from agent.run where run_id = 'agent-20261102'"))
        finally:
            scheduler.LOCK = old_lock

    def test_catch_up_runs_missed_mondays_oldest_first(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scheduler.main(["--once", "--database-url", DB, "--now", "2026-10-26T09:00:00", "--catch-up", "1", "--model", self.model_path])
        lines = [l for l in out.getvalue().splitlines() if l.startswith("agent-")]
        self.assertTrue(lines and "agent-20261026" in lines[-1])
        self.assertEqual("ok", q("select status from agent.run where run_id = 'agent-20261026'"))


if __name__ == "__main__":
    unittest.main()
