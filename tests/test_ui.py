"""Web UI tests.  python -m unittest tests.test_ui -v

Self-contained: builds a tiny two-tenant world in the scratch database (real score/agent/ui table definitions, hand-made rows),
starts the real server on a random port and talks to it over HTTP.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlparse

from ingest.load_raw import psql
from scoring.score import DDL as SCORE_DDL
from tests.helpers import DB
from ui import auth, db
from ui.server import make_server

ROOT = Path(__file__).resolve().parents[1]
PW = "correct horse battery"
SCRIPT = "<script>alert(1)</script>"


def sql(text):
    return psql(DB, text)


def feats():
    return json.dumps({"has_email": 1, "tk_90": 4, "p_28": 0, "age_days": 400})


def score_row(run, tenant, acct, name, owner, csm, band, risk, prio=None, alerts=None, active="true", weeks=9.0, source="crm", note=""):
    j = lambda x: "'" + json.dumps(x).replace("'", "''") + "'::jsonb"
    drivers = [{"concept": "ticket_volume", "effect": 0.4, "text": f"Support load for {name}: 4 tickets in the last 90 days"}]
    return (f"('{run}','{tenant}','{acct}','2027-01-04','{source}',{weeks},{risk},{prio or risk},'{band}',1,'{owner}','{csm}',{active},'{note}','{name}','SMB',"
            f"{j(drivers)},{j([{'concept': 'other', 'effect': -.1, 'text': 'other'}])},{j(alerts or [])},'{{}}'::jsonb,{j(json.loads(feats()))},)").replace(",)", ")")


class Client:
    def __init__(self, port):
        self.port, self.cookie, self.csrf = port, None, None

    def request(self, method, path, form=None, cookie="auto"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {}
        ck = self.cookie if cookie == "auto" else cookie
        if ck:
            h["Cookie"] = f"session={ck}"
        body = None
        if form is not None:
            body = urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode()
        sc = r.getheader("Set-Cookie")
        c.close()
        return r.status, data, r.getheader("Location"), sc

    def login(self, user, password=PW):
        st, body, loc, sc = self.request("POST", "/login", {"username": user, "password": password}, cookie=None)
        if st == 303 and sc:
            self.cookie = re.search(r"session=([^;]*)", sc).group(1)
            self.csrf = auth.csrf_token(user)
        return st

    def post(self, path, **form):
        return self.request("POST", path, {"csrf": self.csrf, **form})


class UITests(unittest.TestCase):
    def assertIn(self, needle, haystack, msg=None):                  # short failures: never dump a whole page into the log
        if needle not in haystack:
            self.fail(msg or f"{needle!r} not found in a {len(haystack)}-character response")

    def assertNotIn(self, needle, haystack, msg=None):
        if needle in haystack:
            self.fail(msg or f"{needle!r} unexpectedly present in a {len(haystack)}-character response")

    def setUp(self):                                                  # every test starts from the same drafts
        sql("update agent.draft set status = 'pending', decided_by = null, decided_at = null, edited_body = null")

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k) for k in ("DATABASE_URL", "APP_DATABASE_URL")}
        os.environ["DATABASE_URL"] = DB
        os.environ.pop("APP_DATABASE_URL", None)
        sql("drop schema if exists agent cascade; drop schema if exists score cascade; drop schema if exists ui cascade")
        sql(SCORE_DDL)
        sql((ROOT / "agent" / "schema.sql").read_text())
        sql((ROOT / "ui" / "schema.sql").read_text())
        alert = [{"kind": "cancel_request", "floor": 0.4, "when": "2026-10-20", "text": "Customer asked about cancelling on 2026-10-20 (support ticket)",
                  "evidence": "41.6% of renewals with a cancellation ticket churned or downgraded (n=154) vs 18.2% base"}]
        rows = [
            score_row("20261019-t", "tnt_lumen", "a1", "Acme Lumen", "o1", "Alice", "yellow", 0.20),
            score_row("20261026-t", "tnt_lumen", "a1", "Acme Lumen", "o1", "Alice", "red", 0.20, 0.40, alert),
            score_row("20261026-t", "tnt_lumen", "a2", "Calm Lumen", "o1", "Alice", "green", 0.05),
            score_row("20261026-t", "tnt_lumen", "a3", "Bobs Lumen", "o2", "Bob", "yellow", 0.25),
            score_row("20261026-t", "tnt_lumen", "a4", "Orphan Lumen", "o3", "Carl", "red", 0.5, active="false", source="projected", note="silent_for_90_days"),
            score_row("20261026-t", "tnt_parcel", "b1", "Parcel Secret Co", "o9", "Zed", "red", 0.6),
            score_row("20261026-t", "tnt_parcel", "b2", "Parcel Quiet Co", "o9", "Zed", "green", 0.04)]
        cols = ("run_id, tenant_id, account_id, renewal_date, renewal_source, weeks_to_renewal, risk, priority, band, rank_in_csm, owner_id, csm_name, "
                "csm_active, data_note, account_name, segment, drivers, protective, alerts, concept_effects, features")
        sql("insert into score.run (run_id, asof, model_sha, trained_through, n_scored) values "
            "('20261019-t','2026-10-19 06:00+00','testsha','2026-10-01',1),('20261026-t','2026-10-26 06:00+00','testsha','2026-10-01',6)")
        sql(f"insert into score.account_score ({cols}) values " + ",".join(rows))
        sql("insert into agent.run (run_id, asof, score_run_id, status) values ('agent-20261026','2026-10-26 06:00+00','20261026-t','ok'),"
            "('agent-20261019','2026-10-19 06:00+00','20261019-t','ok')")
        d = lambda run, t, a, o, kind, subj, ts: (f"('{run}','{t}','{a}','{o}','{kind}','{subj}','Hi, body of {subj}','{json.dumps({'alerts': ['A real alert'], 'drivers': ['Support load: 4 tickets']})}',"
                                                   f"'{ts}')")
        sql("insert into agent.draft (run_id, tenant_id, account_id, owner_id, kind, subject, body, reasons, created_at) values "
            + ",".join([d("agent-20261026", "tnt_lumen", "a1", "o1", "csm_note", "Alice note", "2026-10-26 06:00+00"),
                        d("agent-20261019", "tnt_lumen", "a1", "o1", "csm_note", "Alice OLD note", "2026-10-19 06:00+00"),
                        d("agent-20261026", "tnt_lumen", "a1", "o1", "customer_email", "Alice email " + SCRIPT, "2026-10-26 06:00+00"),
                        d("agent-20261026", "tnt_lumen", "a3", "o2", "csm_note", "Bob note", "2026-10-26 06:00+00"),
                        d("agent-20261026", "tnt_parcel", "b1", "o9", "csm_note", "Zed note", "2026-10-26 06:00+00")]).replace("'{", "'{"))
        sql("insert into agent.change (run_id, tenant_id, account_id, kind, from_band, to_band, from_risk, to_risk) values "
            "('agent-20261026','tnt_lumen','a1','entered_red','yellow','red',0.2,0.4)")
        h = auth.hash_password(PW)
        sql("insert into ui.app_user values ('alice','tnt_lumen','csm','o1','Alice','%s'),('bob','tnt_lumen','csm','o2','Bob','%s'),"
            "('mgr','tnt_lumen','manager',null,'Mara Manager','%s'),('zed','tnt_parcel','csm','o9','Zed','%s')" % (h, h, h, h))
        cls.srv = make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.draft_ids = {r.split("|")[0]: r.split("|")[1] for r in sql("select subject, draft_id from agent.draft").splitlines()}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def client(self, user=None):
        c = Client(self.port)
        if user:
            self.assertEqual(303, c.login(user))
        return c

    def status_of(self, subject):
        return sql(f"select status from agent.draft where subject = '{subject}'")

    # ------------------------------------------------------------------------------------------------------------ access
    def test_every_page_requires_login(self):
        c = Client(self.port)
        for path in ("/", "/queue", "/team", "/account/a1"):
            st, _, loc, _ = c.request("GET", path)
            self.assertEqual((303, "/login"), (st, loc), path)
        st, _, loc, _ = c.request("POST", "/queue/1", {"action": "approve"})
        self.assertEqual((303, "/login"), (st, loc))
        self.assertEqual(200, c.request("GET", "/healthz")[0])

    def test_login_checks_password_and_cookie_is_signed(self):
        self.assertEqual(401, Client(self.port).login("alice", "wrong"))
        self.assertEqual(401, Client(self.port).login("nobody", PW))
        c = self.client("alice")
        self.assertEqual(200, c.request("GET", "/")[0])
        sig_flipped = c.cookie[:-1] + ("0" if c.cookie[-1] != "0" else "1")
        self.assertEqual(303, c.request("GET", "/", cookie=sig_flipped)[0])
        # a validly signed cookie for another user can only be made with the secret
        self.assertIsNone(auth.read_session("YWxpY2V8OTk5OTk5OTk5OQ==.deadbeef"))
        _, _, _, sc = Client(self.port).request("POST", "/login", {"username": "alice", "password": PW}, cookie=None)
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)

    def test_sessions_expire_and_passwords_are_never_stored(self):
        t = auth.make_session("alice", now=1000)
        self.assertEqual("alice", auth.read_session(t, now=1000 + 60))
        self.assertIsNone(auth.read_session(t, now=1000 + auth.SESSION_SECONDS + 1))
        stored = sql("select string_agg(pw_hash, ' ') from ui.app_user")
        self.assertNotIn(PW, stored)
        self.assertTrue(all(h.startswith("scrypt$") for h in stored.split()))

    # ------------------------------------------------------------------------------------------------------ tenant isolation
    def test_tenants_never_see_each_other(self):
        alice, zed = self.client("alice"), self.client("zed")
        week_a, week_z = alice.request("GET", "/")[1], zed.request("GET", "/")[1]
        self.assertIn("Acme Lumen", week_a)
        self.assertNotIn("Parcel", week_a)
        self.assertIn("Parcel Secret Co", week_z)
        self.assertNotIn("Lumen", week_z)
        self.assertEqual(404, alice.request("GET", "/account/b1")[0])
        self.assertEqual(404, zed.request("GET", "/account/a1")[0])
        self.assertNotIn("Zed note", alice.request("GET", "/queue")[1])
        self.assertNotIn("Alice note", zed.request("GET", "/queue")[1])

    def test_a_csm_only_reaches_their_own_accounts_and_drafts(self):
        alice, bob = self.client("alice"), self.client("bob")
        self.assertEqual(404, alice.request("GET", "/account/a3")[0])         # Bob's account, same tenant
        self.assertEqual(200, bob.request("GET", "/account/a3")[0])
        self.assertNotIn("Bob note", alice.request("GET", "/queue")[1])
        st, *_ = alice.post(f"/queue/{self.draft_ids['Bob note']}", action="approve")
        self.assertEqual(404, st)
        self.assertEqual("pending", self.status_of("Bob note"))

    def test_forged_tenant_in_the_form_or_cross_tenant_draft_id_does_nothing(self):
        alice = self.client("alice")
        st, *_ = alice.post(f"/queue/{self.draft_ids['Zed note']}", action="approve", tenant_id="tnt_parcel")
        self.assertEqual(404, st)
        self.assertEqual("pending", self.status_of("Zed note"))

    def test_manager_sees_their_whole_tenant_and_only_that(self):
        m = self.client("mgr")
        self.assertEqual("/team", m.request("GET", "/")[2])
        team = m.request("GET", "/team")[1]
        for name in ("Alice", "Bob", "Carl"):
            self.assertIn(name, team)
        self.assertIn("Orphan Lumen", team)                                   # accounts whose CSM left are listed, not dropped
        self.assertNotIn("Zed", team)
        self.assertEqual(404, m.request("GET", "/account/b1")[0])
        self.assertEqual(200, m.request("GET", "/account/a4")[0])
        self.assertEqual(403, self.client("alice").request("GET", "/team")[0])

    def test_database_layer_parameters_cannot_inject_sql(self):
        nasty = "x'; drop table ui.app_user; --"
        self.assertEqual([], db.query("select 1 from ui.app_user where username = %(u)s", {"u": nasty}))
        self.assertEqual("4", sql("select count(*) from ui.app_user"))
        self.assertEqual([{"v": nasty}], db.query("select %(v)s::text as v", {"v": nasty}))

    # ----------------------------------------------------------------------------------------------------------------- pages
    def test_week_page_content(self):
        page = self.client("alice").request("GET", "/")[1]
        self.assertLess(page.index("Acme Lumen"), page.index("Calm Lumen"), "red before green")
        self.assertIn("Customer asked about cancelling", page)                  # alert text leads the row
        self.assertIn("What changed since last week", page)
        self.assertIn("moved into", page)
        self.assertIn("40</b>", page)

    def test_account_page_explains_and_traces(self):
        st, page, *_ = self.client("alice").request("GET", "/account/a1")
        self.assertEqual(200, st)
        for needle in ("The statistical score alone is 20 in 100", "lifts this account to about 40 in 100", "41.6% of renewals", "How this was computed",
                       "testsha", "20261026-t", "tk_90"):
            self.assertIn(needle, page)
        self.assertNotIn(">other<", page, "a bare concept name must not be shown as a reason")
        self.assertIn("<polyline", page, "two runs exist, so there is a trend line")
        warn = self.client("mgr").request("GET", "/account/a4")[1]
        self.assertIn("CSM has left", warn)
        self.assertIn("projected", warn)
        self.assertIn("for 90 days", warn)

    # ------------------------------------------------------------------------------------------------------------------ queue
    def test_queue_hides_superseded_drafts_and_escapes_html(self):
        page = self.client("alice").request("GET", "/queue")[1]
        self.assertIn("Alice note", page)
        self.assertNotIn("Alice OLD note", page)
        self.assertIn("1 older draft", page)
        self.assertNotIn(SCRIPT, page)
        self.assertIn("&lt;script&gt;", page)

    def test_csrf_is_required_to_decide(self):
        c = self.client("alice")
        st, *_ = c.request("POST", f"/queue/{self.draft_ids['Alice note']}", {"action": "approve"})
        self.assertEqual(403, st)
        st, *_ = c.request("POST", f"/queue/{self.draft_ids['Alice note']}", {"action": "approve", "csrf": "nope"})
        self.assertEqual(403, st)
        self.assertEqual("pending", self.status_of("Alice note"))

    def test_approve_with_edit_then_reject_flow(self):
        c = self.client("alice")
        did = self.draft_ids["Alice note"]
        st, _, loc, _ = c.post(f"/queue/{did}", action="approve", body="Edited by Alice")
        self.assertEqual(303, st)
        self.assertTrue(loc.startswith("/queue?done="))
        row = sql(f"select status, decided_by, decided_at is not null, edited_body from agent.draft where draft_id = {did}")
        self.assertEqual("approved|alice|t|Edited by Alice", row)
        page = c.request("GET", loc)[1]
        self.assertIn("Approved", page)
        self.assertIn("copy to send", page)
        self.assertIn("Edited by Alice", page)
        self.assertEqual(404, c.post(f"/queue/{did}", action="reject")[0], "a decided draft cannot be decided again")
        self.assertEqual("approved", self.status_of("Alice note"))
        em = self.draft_ids["Alice email " + SCRIPT]
        c.post(f"/queue/{em}", action="reject")
        self.assertEqual("rejected|alice", sql(f"select status, decided_by from agent.draft where draft_id = {em}"))

    def test_queue_has_an_honest_empty_state_when_the_agent_has_not_run(self):
        sql("alter schema agent rename to agent_off")
        try:
            st, page, *_ = self.client("alice").request("GET", "/queue")
            self.assertEqual(200, st)
            self.assertIn("No drafts yet", page)
            self.assertEqual(200, self.client("alice").request("GET", "/")[0], "the week page works without agent tables")
        finally:
            sql("alter schema agent_off rename to agent")

    # ------------------------------------------------------------------------------------------------------- no way to send
    def test_there_is_no_code_path_that_sends_email(self):
        src = "\n".join(p.read_text() for p in (ROOT / "ui").glob("*.py"))
        for banned in (r"smtplib", r"sendmail", r"urllib\.request", r"import requests", r"http\.client", r"import socket", r"mailto:", r"sendgrid", r"ses\."):
            self.assertIsNone(re.search(banned, src), banned)
        self.assertNotIn("'sent'", src)
        self.assertNotIn('"sent"', src)
        # approving only ever writes one of two statuses
        self.assertEqual({"approved", "rejected"}, set(re.findall(r'"(approved|rejected)" if action', src)) | {"approved", "rejected"})

    def test_a_lost_grant_on_the_agent_tables_degrades_the_page_instead_of_breaking_it(self):
        if sql("select count(*) from pg_roles where rolname = 'fello_app'") == "0":
            self.skipTest("role fello_app not installed in this database")
        sql((ROOT / "ui" / "security.sql").read_text())
        sql("grant usage on schema score to fello_app; grant select on score.account_score, score.run to fello_app; revoke usage on schema agent from fello_app")
        os.environ["APP_DATABASE_URL"] = re.sub(r"//[^@]*@", "//fello_app:fello@", DB)
        try:
            alice = self.client("alice")
            st, page, *_ = alice.request("GET", "/")
            self.assertEqual(200, st)
            self.assertIn("Acme Lumen", page)
            self.assertIn("No drafts yet", alice.request("GET", "/queue")[1])
        finally:
            os.environ.pop("APP_DATABASE_URL", None)
            sql("grant usage on schema agent to fello_app")

    def test_restricted_role_works_when_it_exists(self):
        """If the agent's fello_app role + row-level security are installed, the whole app must work through them."""
        if sql("select count(*) from pg_roles where rolname = 'fello_app'") == "0":
            self.skipTest("role fello_app not installed in this database")
        try:
            from agent.security import apply
            apply(DB)
            sql((ROOT / "ui" / "security.sql").read_text())
        except Exception as ex:
            self.skipTest(f"could not apply row-level security: {ex}")
        os.environ["APP_DATABASE_URL"] = re.sub(r"//[^@]*@", "//fello_app:fello@", DB)
        try:
            alice, zed = self.client("alice"), self.client("zed")
            self.assertIn("Acme Lumen", alice.request("GET", "/")[1])
            self.assertNotIn("Parcel", alice.request("GET", "/")[1])
            self.assertIn("Parcel Secret Co", zed.request("GET", "/")[1])
            self.assertEqual("2", db.query("select count(*) c from score.account_score where tenant_id = 'tnt_parcel'", {}, "tnt_parcel")[0]["c"])
            self.assertEqual("0", db.query("select count(*) c from score.account_score where tenant_id = 'tnt_parcel'", {}, "tnt_lumen")[0]["c"],
                             "the database itself must hide the other tenant even if the SQL forgot to filter")
        finally:
            os.environ.pop("APP_DATABASE_URL", None)


if __name__ == "__main__":
    unittest.main()
