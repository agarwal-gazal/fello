"""Staging tests.  python -m unittest tests.test_staging -v"""
from __future__ import annotations

import unittest

from tests.helpers import build_dataset, cleanup, load_and_stage, q, truth_links


class StagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.data = build_dataset()
        load_and_stage(cls.data)
        cls.links = truth_links(cls.data)

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    # ------------------------------------------------------------------ dedupe
    def test_product_events_deduped_to_one_row_per_event_id(self):
        raw_distinct = q("select count(distinct (tenant_id, payload->>'event_id')) from raw.product_events")
        self.assertEqual(raw_distinct, q("select count(*) from stg.product_event"))
        self.assertEqual("0", q("select count(*) - count(distinct (tenant_id, event_id)) from stg.product_event"))

    def test_known_at_is_first_arrival_not_last(self):
        bad = q("""select count(*) from stg.product_event s join (
                       select tenant_id, payload->>'event_id' as id, min(synced_at) as first_seen
                       from raw.product_events group by 1, 2) r on r.tenant_id = s.tenant_id and r.id = s.event_id
                   where s.known_at <> r.first_seen""")
        self.assertEqual("0", bad)
        self.assertGreater(int(q("select count(*) from (select 1 from raw.stripe_events group by tenant_id, payload->>'id' having count(*) > 1) x")), 0,
                           "fixture must actually contain duplicates for this test to mean anything")

    def test_ticket_versions_collapse_to_latest_and_keep_first_known_at(self):
        self.assertEqual("0", q("select count(*) - count(distinct (tenant_id, ticket_id)) from stg.zd_ticket"))
        multi = q("select count(*) from (select 1 from raw.zendesk_tickets group by tenant_id, payload->>'id' having count(*) > 1) x")
        self.assertGreater(int(multi), 0)
        # the staged status is the status of the version with the latest updated_at
        wrong = q("""select count(*) from stg.zd_ticket t join lateral (
                         select status from stg.zd_ticket_version v where v.tenant_id = t.tenant_id and v.ticket_id = t.ticket_id
                         order by updated_at desc, synced_at desc limit 1) v on true where v.status <> t.status""")
        self.assertEqual("0", wrong)
        early = q("""select count(*) from stg.zd_ticket t join lateral (
                         select min(synced_at) m from stg.zd_ticket_version v where v.tenant_id = t.tenant_id and v.ticket_id = t.ticket_id) v on true
                     where v.m <> t.known_at""")
        self.assertEqual("0", early)

    def test_stripe_event_retries_collapsed(self):
        self.assertEqual(q("select count(distinct (tenant_id, payload->>'id')) from raw.stripe_events"), q("select count(*) from stg.stripe_event"))

    # ------------------------------------------------------------------ time
    def test_no_event_is_dated_after_it_was_received(self):
        self.assertEqual("0", q("select count(*) from stg.product_event where event_ts > known_at"))

    def test_zoneless_and_future_timestamps_are_flagged_not_hidden(self):
        bad = q("""select count(*) from stg.product_event s join raw.product_events r
                     on r.tenant_id = s.tenant_id and r.payload->>'event_id' = s.event_id and r.synced_at = s.known_at
                   where r.payload->>'timestamp' !~ 'Z$' and s.ts_quality = 'ok'""")
        self.assertEqual("0", bad)
        self.assertGreater(int(q("select count(*) from stg.product_event where ts_quality = 'naive_assumed_utc'")), 0)

    # ------------------------------------------------------------------ noise exclusion is rule-based yet matches the truth
    def test_test_mode_customers_flagged_exactly(self):
        truth = set(self.links[(self.links.source_entity == "stripe_customer") & (self.links.true_account_id == "NOISE")].source_record_id)
        flagged = set(q("select customer_id from stg.stripe_customer where is_test").split())
        self.assertEqual(truth, flagged)

    def test_internal_workspaces_flagged_exactly(self):
        truth = set(self.links[(self.links.source_entity == "product_workspace") & (self.links.true_account_id == "NOISE")].source_record_id)
        flagged = set(q("select workspace_id from stg.product_workspace where is_internal").split())
        self.assertEqual(truth, flagged)
        self.assertGreater(int(q("select count(*) from stg.product_event where is_internal")), 0)

    # ------------------------------------------------------------------ gmail
    def test_gmail_direction_and_bounces(self):
        inbound = q("select count(*) from raw.gmail_messages where payload->'labelIds' ? 'INBOX'")
        self.assertEqual(inbound, q("select count(*) from stg.gmail_message where direction = 'inbound'"))
        raw_bounce = q("select count(*) from raw.gmail_messages where payload->>'snippet' like '%couldn''t be delivered%'")
        self.assertEqual(raw_bounce, q("select count(*) from stg.gmail_message where is_bounce"))
        self.assertEqual("0", q("select count(*) from stg.gmail_message where is_bounce and counterparty_email like 'mailer-daemon@%'"))

    def test_free_mail_domains_are_registered(self):
        self.assertEqual("1", q("select count(*) from stg.free_mail_domain where domain = 'gmail.com'"))


if __name__ == "__main__":
    unittest.main()
