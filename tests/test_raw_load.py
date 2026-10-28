"""Tests for the raw loader. Run:  python -m unittest tests.test_raw_load -v

Needs a scratch Postgres database (default postgresql://fello:fello@localhost/fello_test, override with TEST_DATABASE_URL).
Builds a tiny dataset with the real generator, so the tests exercise the real shape of the data, not hand-made fixtures.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from data_gen import generate
from ingest import load_raw
from ingest.load_raw import psql

DB = os.environ.get("TEST_DATABASE_URL", "postgresql://fello:fello@localhost/fello_test")


def q(sql: str) -> str:
    return psql(DB, sql)


def run_load(data: Path):
    return load_raw.main(["--data", str(data), "--database-url", DB])


class RawLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fello_raw_test_"))
        cls.data = cls.tmp / "data"
        generate.main(["--out", str(cls.data), "--seed", "3", "--scale", "0.02"])
        cls.files = sorted((cls.data / "raw").glob("*/*/*.jsonl"))
        cls.manifest = json.loads((cls.data / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        q("drop schema if exists raw cascade")
        run_load(self.data)

    # ------------------------------------------------------------------ correctness
    def test_row_counts_match_every_file(self):
        for f in self.files:
            tenant, source, entity = f.parent.parent.name, f.parent.name, f.stem
            n_file = sum(1 for _ in open(f))
            n_db = int(q(f"select count(*) from raw.{source}_{entity} where tenant_id = '{tenant}'"))
            self.assertEqual(n_file, n_db, f"{tenant} {source}.{entity}")
        self.assertEqual(self.manifest["total_rows"], int(q("select sum(rows_new) from raw.load_log")))

    def test_payload_is_verbatim(self):
        f = next(p for p in self.files if p.stem == "events" and p.parent.name == "product")
        lines = open(f).read().splitlines()[:200]
        stored = {r.split("|", 1)[0]: json.loads(r.split("|", 1)[1]) for r in
                  q(f"select content_hash || '|' || payload::text from raw.product_events "
                    f"where tenant_id = '{f.parent.parent.name}'").splitlines()}
        import hashlib
        for line in lines:
            self.assertEqual(json.loads(line), stored[hashlib.md5(line.encode()).hexdigest()])

    def test_synced_at_typed_and_matches_payload(self):
        bad = q("select count(*) from raw.stripe_events where synced_at <> (payload->>'_synced_at')::timestamptz")
        self.assertEqual("0", bad)

    def test_no_record_arrives_after_export_cutoff(self):
        cutoff = self.manifest["export_at"]
        for t in ("product_events", "gmail_messages", "zendesk_tickets", "stripe_events", "stripe_invoices"):
            self.assertEqual("0", q(f"select count(*) from raw.{t} where synced_at > '{cutoff}'"), t)

    def test_source_duplicates_are_kept_not_collapsed(self):
        """Webhook retries / re-sent events differ by _synced_at, so raw must keep them (staging dedupes)."""
        rows, distinct = q("select count(*), count(distinct payload->>'event_id') from raw.product_events").split("|")
        self.assertGreater(int(rows), int(distinct))
        rows, distinct = q("select count(*), count(distinct payload->>'id') from raw.stripe_events").split("|")
        self.assertGreater(int(rows), int(distinct))

    def test_tenants_stay_separate(self):
        for t in self.manifest["tenants"]:
            expected = sum(self.manifest["tenants"][t]["rows"].values())
            got = sum(int(q(f"select count(*) from raw.{tbl} where tenant_id = '{t}'")) for tbl in
                      ("salesforce_accounts", "salesforce_contacts", "salesforce_opportunities", "salesforce_users",
                       "stripe_customers", "stripe_subscriptions", "stripe_invoices", "stripe_events",
                       "zendesk_organizations", "zendesk_tickets", "product_events", "product_workspaces",
                       "product_users", "gmail_messages"))
            self.assertEqual(expected, got, t)

    # ------------------------------------------------------------------ idempotency
    def test_reload_is_a_noop(self):
        before = q("select count(*) from raw.product_events")
        run_load(self.data)
        self.assertEqual(before, q("select count(*) from raw.product_events"))
        self.assertEqual("0", q("select coalesce(sum(rows_new),0) from raw.load_log where load_id > "
                                "(select max(load_id) - 41 from raw.load_log)"))

    def test_incremental_load_adds_only_new_records(self):
        data2 = self.tmp / "data2"
        if data2.exists():
            shutil.rmtree(data2)
        shutil.copytree(self.data / "raw", data2 / "raw")
        p = next((data2 / "raw").glob("*/zendesk/tickets.jsonl"))
        lines = open(p).read().splitlines()
        newer = json.loads(lines[0])
        newer["status"], newer["_synced_at"] = "solved", "2026-10-26T12:00:00Z"   # a later version of a ticket
        with open(p, "a") as fh:
            fh.write(json.dumps(newer, separators=(",", ":")) + "\n")
        before = int(q("select count(*) from raw.zendesk_tickets"))
        run_load(data2)
        self.assertEqual(before + 1, int(q("select count(*) from raw.zendesk_tickets")))

    # ------------------------------------------------------------------ failure modes
    def test_record_without_synced_at_fails_loudly_and_loads_nothing_from_that_file(self):
        data3 = self.tmp / "data3"
        (data3 / "raw" / "tnt_x" / "zendesk").mkdir(parents=True, exist_ok=True)
        good = {"id": 1, "_synced_at": "2026-01-01T00:00:00Z"}
        bad = {"id": 2}
        with open(data3 / "raw" / "tnt_x" / "zendesk" / "tickets.jsonl", "w") as fh:
            fh.write(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
        with self.assertRaises(Exception):
            run_load(data3)
        self.assertEqual("0", q("select count(*) from raw.zendesk_tickets where tenant_id = 'tnt_x'"))
        self.assertEqual("0", q("select count(*) from raw.load_log where tenant_id = 'tnt_x'"))

    def test_unknown_entity_is_rejected_not_ignored(self):
        data4 = self.tmp / "data4"
        (data4 / "raw" / "tnt_x" / "salesforce").mkdir(parents=True, exist_ok=True)
        (data4 / "raw" / "tnt_x" / "salesforce" / "leads.jsonl").write_text('{"_synced_at":"2026-01-01T00:00:00Z"}\n')
        with self.assertRaises(ValueError):
            run_load(data4)

    def test_backslashes_and_quotes_survive_the_copy(self):
        data5 = self.tmp / "data5"
        (data5 / "raw" / "tnt_x" / "zendesk").mkdir(parents=True, exist_ok=True)
        rec = {"id": 9, "subject": 'path C:\\temp\\new "quoted" \\n tab\\t', "_synced_at": "2026-01-01T00:00:00Z"}
        (data5 / "raw" / "tnt_x" / "zendesk" / "tickets.jsonl").write_text(json.dumps(rec) + "\n")
        run_load(data5)
        got = json.loads(q("select payload::text from raw.zendesk_tickets where tenant_id = 'tnt_x'"))
        self.assertEqual(rec, got)


if __name__ == "__main__":
    unittest.main()
