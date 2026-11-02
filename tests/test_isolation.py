"""Tenant isolation tests.  python -m unittest tests.test_isolation -v

Connects as the real application role (fello_app: not superuser, no BYPASSRLS) and tries to read or change another tenant's data
by every route we could think of. Also checks the role itself, so a future 'GRANT ... TO fello_app' mistake fails here.
"""
from __future__ import annotations

import re
import unittest

from agent import security
from agent.db import ensure_schema, psql
from scoring.score import DDL as SCORE_DDL
from tests.helpers import DB, q

APP_DB = re.sub(r"//[^@]+@", "//fello_app:fello@", DB)


def app(tenant: str | None, sql: str) -> list[str]:
    """Run SQL as fello_app with the tenant set the way the web app would (set_config on its connection)."""
    pre = "" if tenant is None else f"select set_config('app.tenant_id', $t${tenant}$t$, false) as _t \\gset\n"
    return psql(APP_DB, pre + sql).splitlines()


class IsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        psql(DB, "drop schema if exists agent cascade; drop schema if exists score cascade")
        ensure_schema(DB)
        psql(DB, SCORE_DDL)
        ins_score = lambda t, a: (f"insert into score.account_score (run_id, tenant_id, account_id, risk, band) values ('r1', '{t}', '{a}', 0.5, 'red');")
        psql(DB, f"""
            insert into score.run (run_id, asof, model_sha) values ('r1', now(), 'x');
            {ins_score('tnt_a', 'acc_1')} {ins_score('tnt_a', 'acc_shared')} {ins_score('tnt_b', 'acc_2')} {ins_score('tnt_b', 'acc_shared')}
            insert into agent.run (run_id, asof, score_run_id, status) values ('agent-1', now(), 'r1', 'ok'), ('agent-failed', now(), null, 'failed');
            insert into agent.change (run_id, tenant_id, account_id, kind) values ('agent-1', 'tnt_a', 'acc_1', 'entered_red'), ('agent-1', 'tnt_b', 'acc_2', 'entered_red');
            insert into agent.draft (draft_id, run_id, tenant_id, account_id, kind, subject, body, reasons, created_at) values
                (101, 'agent-1', 'tnt_a', 'acc_1', 'csm_note', 's', 'secret of A', '{{}}', now()),
                (201, 'agent-1', 'tnt_b', 'acc_2', 'csm_note', 's', 'secret of B', '{{}}', now()),
                (102, 'agent-1', 'tnt_a', 'acc_shared', 'csm_note', 's', 'shared id, A', '{{}}', now()),
                (202, 'agent-1', 'tnt_b', 'acc_shared', 'csm_note', 's', 'shared id, B', '{{}}', now());""")
        security.apply(DB)

    @classmethod
    def tearDownClass(cls):
        psql(DB, "drop schema if exists agent cascade; drop schema if exists score cascade")

    def test_app_role_is_not_privileged(self):
        r = q("select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb from pg_roles where rolname = 'fello_app'")
        self.assertEqual("f|f|f|f", r)
        self.assertEqual("f", q("select pg_has_role('fello_app', 'fello', 'member')"), "the app role must not be able to become the pipeline role")

    def test_each_tenant_sees_only_its_own_rows_in_every_table(self):
        for tenant, other in (("tnt_a", "tnt_b"), ("tnt_b", "tnt_a")):
            for table in ("score.account_score", "agent.change", "agent.draft"):
                self.assertEqual(["0"], app(tenant, f"select count(*) from {table} where tenant_id = '{other}'"), (tenant, table))
                self.assertEqual(["1"], app(tenant, f"select count(distinct tenant_id) from {table}"), (tenant, table))

    def test_cannot_reach_another_tenant_by_guessing_ids(self):
        self.assertEqual([], app("tnt_a", "select body from agent.draft where draft_id = 201"))
        self.assertEqual([], app("tnt_a", "select body from agent.draft where body like '%secret of B%'"))
        # an account id that exists in both tenants returns only the caller's own row
        self.assertEqual(["shared id, A"], app("tnt_a", "select body from agent.draft where account_id = 'acc_shared'"))
        self.assertEqual(["shared id, B"], app("tnt_b", "select body from agent.draft where account_id = 'acc_shared'"))

    def test_joins_unions_and_subqueries_do_not_widen_the_view(self):
        self.assertEqual(["2"], app("tnt_a", "select count(*) from score.account_score s join agent.draft d on d.account_id = s.account_id"))
        self.assertEqual(["4"], app("tnt_a", "select count(*) from (select account_id from score.account_score union all select account_id from score.account_score) x"))
        self.assertEqual(["2"], app("tnt_a", "select count(*) from score.account_score where account_id in (select account_id from agent.draft)"))
        self.assertEqual(["0"], app("tnt_a", "select count(*) from agent.draft d where exists (select 1 from agent.draft e where e.tenant_id = 'tnt_b')"))

    def test_run_tables_without_a_tenant_column_do_not_leak(self):
        self.assertEqual(["1"], app("tnt_a", "select count(*) from score.run"))
        self.assertEqual(["agent-1"], app("tnt_a", "select run_id from agent.run"), "a failed run with no score rows is operator-only")
        self.assertEqual(["0"], app("tnt_c", "select count(*) from score.run"))
        self.assertEqual(["0"], app("tnt_c", "select count(*) from agent.run"))

    def test_no_tenant_or_a_bogus_one_sees_nothing(self):
        for t in (None, "", "tnt_c", "tnt_a' or '1'='1", "%", "tnt_%"):
            for table in ("score.account_score", "score.run", "agent.change", "agent.draft", "agent.run"):
                self.assertEqual(["0"], app(t, f"select count(*) from {table}"), (t, table))

    def test_cannot_update_another_tenants_draft(self):
        self.assertEqual([], app("tnt_a", "update agent.draft set status = 'approved' where draft_id = 201 returning draft_id"))
        self.assertEqual([], app("tnt_a", "update agent.draft set status = 'approved' where tenant_id = 'tnt_b' returning draft_id"))
        self.assertEqual("pending", q("select status from agent.draft where draft_id = 201"))
        self.assertEqual("pending", q("select status from agent.draft where draft_id = 202"))

    def test_can_decide_its_own_draft_but_only_the_decision_columns(self):
        self.assertEqual(["101"], app("tnt_a", "update agent.draft set status = 'approved', decided_by = 'gazal', decided_at = now(), edited_body = 'ok' "
                                             "where draft_id = 101 returning draft_id"))
        self.assertEqual("approved|gazal", q("select status || '|' || decided_by from agent.draft where draft_id = 101"))
        for sql in ("update agent.draft set body = 'tampered' where draft_id = 102",
                    "update agent.draft set tenant_id = 'tnt_b' where draft_id = 102",
                    "update agent.draft set reasons = '{}' where draft_id = 102"):
            with self.assertRaises(RuntimeError, msg=sql):
                app("tnt_a", sql)
        self.assertEqual("shared id, A", q("select body from agent.draft where draft_id = 102"))

    def test_app_role_cannot_insert_delete_or_touch_other_schemas(self):
        for sql in ("insert into agent.draft (run_id, tenant_id, account_id, kind, subject, body, reasons, created_at) values ('agent-1','tnt_a','x','csm_note','s','b','{}',now())",
                    "delete from agent.draft", "delete from score.account_score", "update score.account_score set risk = 0",
                    "insert into agent.change (run_id, tenant_id, account_id, kind) values ('agent-1','tnt_a','z','left_red')",
                    "select * from core.account limit 1", "select * from stg.sf_account limit 1", "select * from raw.load_log limit 1",
                    "alter table agent.draft disable row level security", "drop policy tenant_isolation on agent.draft"):
            with self.assertRaises(RuntimeError, msg=sql):
                app("tnt_a", sql)

    def test_applying_security_twice_changes_nothing(self):
        before = q("select count(*) from pg_policies where schemaname in ('agent','score')")
        security.apply(DB)
        self.assertEqual(before, q("select count(*) from pg_policies where schemaname in ('agent','score')"))
        self.assertEqual(["1"], app("tnt_a", "select count(distinct tenant_id) from agent.draft"))

    def test_pipeline_role_still_sees_and_writes_everything(self):
        self.assertEqual("4", q("select count(*) from agent.draft where status in ('pending','approved')"))
        q("insert into agent.change (run_id, tenant_id, account_id, kind) values ('agent-1', 'tnt_b', 'pipeline-write', 'left_red')")
        self.assertEqual("1", q("select count(*) from agent.change where account_id = 'pipeline-write'"))


if __name__ == "__main__":
    unittest.main()
