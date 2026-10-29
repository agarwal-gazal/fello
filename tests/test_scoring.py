"""Scoring tests.  python -m unittest tests.test_scoring -v"""
from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

from core import build_core
from features import build_features, features
from model import train
from model.design import design
from model.explain import contributions
from resolve import resolve as er
from scoring import score as sc
from scoring.alerts import alerts_for
from tests.helpers import DB, build_dataset, cleanup, load_and_stage, q

ASOF = pd.Timestamp("2026-09-28")


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.data = build_dataset(scale=0.1, seed=5)
        load_and_stage(cls.data)
        er.run(DB)
        m = json.loads((cls.data / "manifest.json").read_text())
        build_core.build(DB, m["export_at"])
        build_features.main(["--database-url", DB, "--data", str(cls.data)])
        D = train.load(DB)
        pipe, cols = train.fit_final(D, pd.Timestamp("2026-06-01"))
        cls.model = {"model": pipe, "columns": cols, "trained_through": "2026-06-01", "sha": "unittest"}
        cls.prepared = features.prepare(build_features.load_tables(DB))
        cls.pop = sc.population(DB, ASOF)
        cls.scored = sc.score(cls.pop, ASOF, cls.model, cls.prepared)
        cls.run_id = sc.persist(DB, ASOF, cls.model, cls.scored)

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def test_population_is_current_customers_renewing_within_horizon(self):
        p = self.pop
        self.assertGreater(len(p), 20)
        self.assertTrue((p.renewal_date > ASOF).all())
        self.assertTrue((p.renewal_date <= ASOF + pd.Timedelta(weeks=sc.HORIZON_WEEKS)).all())
        self.assertFalse(p.duplicated(["tenant_id", "account_id"]).any())
        gone = q(f"select count(*) from core.renewal where outcome = 'churned' and renewal_date <= '{ASOF.date()}'")
        self.assertGreater(int(gone), 0)
        churned = {tuple(x.split("|")) for x in q(f"select tenant_id||'|'||account_id from core.renewal where outcome = 'churned' and renewal_date <= '{ASOF.date()}'").splitlines()}
        self.assertFalse({(t, a) for t, a in zip(p.tenant_id, p.account_id)} & churned, "a customer that already left must not be scored")

    def test_scores_bands_and_ranks_are_coherent(self):
        s = self.scored
        self.assertTrue(((s.risk > 0) & (s.risk < 1)).all())
        self.assertTrue((s.priority >= s.risk - 1e-12).all())
        self.assertTrue(all(sc.band(p) == b for p, b in zip(s.priority, s.band)))
        for _, g in s.groupby(["tenant_id", "owner_id"]):
            self.assertEqual(list(range(1, len(g) + 1)), sorted(g.rank_in_csm))
            self.assertTrue(g.sort_values("rank_in_csm").priority.is_monotonic_decreasing)

    def test_persisted_risk_is_reproducible_from_stored_features(self):
        """Traceability: a number on a CSM's screen can be recomputed from what the system stored, with the stored model."""
        rows = q(f"select features::text, risk from score.account_score where run_id = '{self.run_id}' order by risk desc limit 25")
        self.assertTrue(rows)
        feats = pd.DataFrame([json.loads(l.split("|")[0]) for l in rows.splitlines()])
        stored = np.array([float(l.rsplit("|", 1)[1]) for l in rows.splitlines()])
        feats["segment"] = [s for s in q(f"select segment from score.account_score where run_id = '{self.run_id}' order by risk desc limit 25").splitlines()]
        again = self.model["model"].predict_proba(design(feats, self.model["columns"])[self.model["columns"]])[:, 1]
        np.testing.assert_allclose(again, stored, atol=2e-5)

    def test_drivers_decompose_the_score_exactly(self):
        sample = self.scored.head(30)
        feats = pd.DataFrame(list(sample.features))
        feats["segment"] = sample.segment.values
        C = contributions(self.model, feats)
        logit = C.sum(axis=1) + self.model["model"][-1].intercept_[0]
        np.testing.assert_allclose(logit, self.model["model"].decision_function(
            design(feats, self.model["columns"])[self.model["columns"]]), atol=1e-9)

    def test_persist_is_idempotent(self):
        before = q(f"select count(*) from score.account_score where run_id = '{self.run_id}'")
        sc.persist(DB, ASOF, self.model, self.scored)
        self.assertEqual(before, q(f"select count(*) from score.account_score where run_id = '{self.run_id}'"))
        self.assertEqual("1", q(f"select count(*) from score.run where run_id = '{self.run_id}'"))

    def test_alert_respects_knowledge_time_and_never_changes_model_risk(self):
        T = lambda s: pd.Timestamp(s)
        tk = pd.DataFrame([{"tenant_id": "t", "account_id": "a", "created_at": T("2026-06-19 14:00"), "known_at": T("2026-06-21 07:00"),
                            "category": "cancellation"}])
        prep = {"ticket": {("t", "a"): tk}, "email": {}, "champion_emails": {}}
        self.assertEqual([], alerts_for(prep, "t", "a", T("2026-06-20")), "ticket exists but the warehouse has not received it yet")
        got = alerts_for(prep, "t", "a", T("2026-06-22"))
        self.assertEqual(["cancel_request"], [x["kind"] for x in got])
        self.assertEqual([], alerts_for(prep, "t", "a", T("2026-09-01")), "a stale request (> 45 days) is not an alert")
        # priority may exceed risk only through an alert
        lifted = self.scored[self.scored.priority > self.scored.risk + 1e-9]
        self.assertTrue(all(len(a) > 0 for a in lifted.alerts))

    def test_departed_csm_accounts_are_visible_not_dropped(self):
        departed = self.scored[~self.scored.csm_active]
        self.assertEqual(len(departed), int(q(f"select count(*) from score.account_score where run_id = '{self.run_id}' and not csm_active")))


if __name__ == "__main__":
    unittest.main()
