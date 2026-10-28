"""Entity-resolution tests.  python -m unittest tests.test_resolve -v

The pipeline never sees the answer key; these tests do, and assert on the properties that matter:
a wrong link poisons an account's features, so precision and 'never link noise' are hard requirements, coverage is a soft one.
"""
from __future__ import annotations

import unittest

import pandas as pd

from resolve import resolve as er
from resolve.io import read_df
from resolve.names import edit_distance, name_key, slug_key
from tests.helpers import DB, build_dataset, cleanup, load_and_stage, q, truth_links


class NameTests(unittest.TestCase):
    def test_edit_distance_catches_typos_not_different_words(self):
        self.assertEqual(1, edit_distance("noblestack", "noblesatck"))           # a transposition is one operation
        self.assertEqual(1, edit_distance("forgecapital", "forgcapital"))        # dropped letter
        self.assertGreater(edit_distance("acmelabs", "acmesystems", cap=1), 1)   # different company names are far apart

    def test_legal_suffixes_and_case_do_not_matter(self):
        self.assertEqual(name_key("Acme Corp."), name_key("ACME, Inc"))
        self.assertEqual(slug_key("Forge Capital"), slug_key("ForgeCapital"))


class ResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.data = build_dataset()
        load_and_stage(cls.data)
        cls.sf_truth = None
        er.run(DB)
        cls.truth = truth_links(cls.data).drop_duplicates()
        link = read_df(DB, "select tenant_id, source_entity, source_record_id, account_id, method, confidence, conflict from er.link")
        link["confidence"] = pd.to_numeric(link["confidence"])
        cls.j = cls.truth.merge(link, on=["tenant_id", "source_entity", "source_record_id"], how="left", validate="one_to_one")
        sf = cls.truth[cls.truth.source_entity == "salesforce_account"].set_index(["tenant_id", "source_record_id"]).true_account_id
        cls.j["pred_true"] = [sf.get((t, a)) if isinstance(a, str) else None for t, a in zip(cls.j.tenant_id, cls.j.account_id)]

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def _entity(self, name):
        return self.j[self.j.source_entity == name]

    def test_noise_is_never_linked(self):
        noise = self.j[(self.j.true_account_id == "NOISE") & (self.j.source_entity != "salesforce_account")]
        self.assertGreater(len(noise), 0, "fixture must contain test-mode / internal records")
        self.assertEqual(0, int(noise.account_id.notna().sum()))

    def test_link_precision_is_near_perfect_for_every_entity(self):
        for ent in ("stripe_customer", "zendesk_org", "product_workspace", "zendesk_ticket", "gmail_message"):
            e = self._entity(ent)
            e = e[(e.true_account_id != "NOISE") & e.account_id.notna()]
            self.assertGreater(len(e), 0, ent)
            self.assertGreaterEqual((e.pred_true == e.true_account_id).mean(), 0.995, ent)

    def test_coverage_is_high_where_company_evidence_exists(self):
        for ent, floor in (("stripe_customer", .98), ("zendesk_org", .98), ("product_workspace", .98), ("zendesk_ticket", .90), ("gmail_message", .95)):
            e = self._entity(ent)
            e = e[e.true_account_id != "NOISE"]
            self.assertGreaterEqual(e.account_id.notna().mean(), floor, ent)

    def test_salesforce_duplicates_merge_but_name_twins_do_not(self):
        sf = self._entity("salesforce_account")
        self.assertLess(sf.account_id.nunique(), len(sf), "fixture must contain duplicate CRM records")
        # never merge two different real companies (this is the name-twin trap)
        per_pred = sf.groupby("account_id").true_account_id.nunique()
        self.assertEqual(0, int((per_pred > 1).sum()))
        # and the duplicates that exist are found
        per_true = sf.groupby("true_account_id").account_id.nunique()
        self.assertLessEqual(int((per_true > 1).sum()), max(1, int(0.02 * len(per_true))))

    def test_personal_email_is_weaker_than_company_evidence(self):
        weak = self.j[self.j.method.fillna("").str.endswith("_freemail")]
        self.assertTrue((weak.confidence < er.WEAK).all())

    def test_every_link_records_method_and_confidence(self):
        linked = self.j[self.j.account_id.notna()]
        self.assertTrue(linked.method.notna().all())
        self.assertTrue(((linked.confidence > 0) & (linked.confidence <= 1)).all())

    def test_rerun_is_deterministic(self):
        before = q("select md5(string_agg(tenant_id||source_entity||source_record_id||coalesce(account_id,'')||coalesce(method,''), ',' order by tenant_id, source_entity, source_record_id)) from er.link")
        er.run(DB)
        after = q("select md5(string_agg(tenant_id||source_entity||source_record_id||coalesce(account_id,'')||coalesce(method,''), ',' order by tenant_id, source_entity, source_record_id)) from er.link")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
