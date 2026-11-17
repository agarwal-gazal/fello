"""CSM web app (standard library only).   python -m ui.server --port 8080

Rules enforced here, each covered by tests/test_ui.py:
  * every page except /login and /healthz requires a valid signed session
  * every query carries the tenant from the session (never from the URL or form), and CSMs only reach their own accounts
  * POSTs need a CSRF token
  * nothing in this app can send email: approving a draft only changes its status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import auth, pages
from .db import DBError, columns, j, query, table_exists

SECURE = os.environ.get("UI_SECURE_COOKIES") == "1"
SHOW_HINT = os.environ.get("SHOW_DEMO_HINT") == "1"
ATTEMPTS: dict = defaultdict(list)
DECIDED_SHOWN = 300                                # approved/rejected drafts kept on the queue page, newest first
LATEST = "select run_id, asof::text as asof, model_sha, trained_through from score.run order by asof desc limit 1"
ACCT_COLS = ("account_id, account_name, band, risk, priority, weeks_to_renewal, renewal_date::text as renewal_date, renewal_source, data_note, "
             "csm_active, csm_name, owner_id, segment, drivers::text as drivers, protective::text as protective, alerts::text as alerts, features::text as features")


# ----------------------------------------------------------------------------------------------------------------- data
def clean(r: dict) -> dict:
    r = dict(r)
    for k in ("drivers", "protective", "alerts"):
        r[k] = j(r.get(k), [])
    r["features"] = j(r.get("features"), {})
    r["csm_active"] = r.get("csm_active") in ("t", "true", True)
    return r


LATEST_OK = ("select s.run_id, s.asof::text as asof, s.model_sha, s.trained_through from score.run s "
             "where exists (select 1 from agent.run a where a.score_run_id = s.run_id and a.status = 'ok') order by s.asof desc limit 1")


def latest_run(tenant_id: str):
    """Newest Monday the agent FINISHED. Scores are saved before the agent writes its changes and drafts, so a run that failed in
    between must not be shown as current. Falls back to the newest score run only when the agent has never run at all."""
    try:
        rows = query(LATEST_OK, {}, tenant_id)       # tenant set: under row-level security a run is visible only through that tenant's scores
        if rows:
            return rows[0]
        if query("select 1 from agent.run limit 1", {}, tenant_id):
            return None                              # the agent has run, but nothing finished successfully for this tenant
    except Exception:                                # agent tables missing or unreadable: show scores without agent features
        pass
    rows = query(LATEST, {}, tenant_id)
    return rows[0] if rows else None


def accounts(user, run, account_id=None):
    sql = f"select {ACCT_COLS} from score.account_score where run_id = %(r)s and tenant_id = %(t)s"
    p = {"r": run["run_id"], "t": user["tenant_id"]}
    if user["role"] == "csm":
        sql += " and owner_id = %(o)s"
        p["o"] = user["owner_id"]
    if account_id:
        sql += " and account_id = %(a)s"
        p["a"] = account_id
    return [clean(r) for r in query(sql + " order by priority desc, risk desc", p, user["tenant_id"])]


def first(d, keys):
    return next((d[k] for k in keys if d.get(k) not in (None, "")), None)


def reasons_text(v):
    v = j(v, v) if isinstance(v, str) and v[:1] in "[{" else v
    if isinstance(v, dict) and ("alerts" in v or "drivers" in v):          # the agent's structured evidence
        parts = list(v.get("alerts") or []) + list(v.get("drivers") or [])[:3]
        return "; ".join(str(x) for x in parts) or None
    if isinstance(v, list):
        return "; ".join(str(x.get("text") if isinstance(x, dict) and x.get("text") else x) for x in v)
    if isinstance(v, dict):
        return "; ".join(str(x) for x in v.values())
    return v


def draft_meta():
    """The agent owns agent.draft; read what exists rather than assume one exact shape. If the tables are missing OR not readable by this
    role (a grant lost after the agent recreated its schema), the pages that use them show their empty state and the cause goes to the log:
    drafts are optional, the weekly list must keep working."""
    try:
        if not table_exists("agent.draft"):
            return None
        cols = columns("agent.draft")
        query("select 1 from agent.draft limit 1", {}, None)
    except DBError as ex:
        sys.stderr.write(f"agent.draft unavailable: {ex}\n")
        return None
    idc = next((c for c in ("draft_id", "id") if c in cols), None)
    return {"cols": cols, "id": idc} if idc and "tenant_id" in cols and "status" in cols else None


def drafts(user, run, account_id=None, only_status=None):
    meta = draft_meta()
    if not meta:
        return None, []
    # EVERY pending draft (one left out could be neither seen nor approved), plus the most recent decided ones for history
    idc, newest_first = meta["id"], ("created_at desc, " if "created_at" in meta["cols"] else "") + meta["id"] + " desc"
    rows = query(f"""select to_jsonb(d)::text as r from agent.draft d where d.tenant_id = %(t)s and (d.status = 'pending' or d.{idc} in
                     (select {idc} from agent.draft where tenant_id = %(t)s and status <> 'pending' order by {newest_first} limit {DECIDED_SHOWN}))""",
                 {"t": user["tenant_id"]}, user["tenant_id"])
    mine = {a["account_id"]: a["account_name"] for a in accounts(user, run)} if run else {}
    out = []
    for r in rows:
        d = json.loads(r["r"])
        acct = d.get("account_id")
        # a CSM sees the drafts addressed to them; if a draft carries no owner, fall back to "an account on my list"
        if user["role"] == "csm":
            owner = d.get("owner_id")
            if (owner is not None and owner != user["owner_id"]) or (owner is None and acct not in mine):
                continue
        if account_id and acct != account_id:
            continue
        out.append({"id": str(d[meta["id"]]), "status": str(d.get("status") or "pending"), "account_id": acct, "account_name": mine.get(acct),
                    "kind": first(d, ("kind", "draft_type", "type", "channel")), "subject": d.get("subject"), "body": d.get("body"), "to": d.get("to_address"),
                    "edited_body": d.get("edited_body"), "created_at": d.get("created_at") or "", "superseded": False, "reasons": reasons_text(first(d, ("reasons", "rationale", "why", "evidence")))})
    newest: dict = {}
    for d in out:                                  # a newer draft (in ANY state) for the same account and kind replaces an older pending one
        k = (d["account_id"], d["kind"])
        newest[k] = max(newest.get(k, ""), d["created_at"])
    for d in out:
        d["superseded"] = d["status"] == "pending" and d["created_at"] < newest[(d["account_id"], d["kind"])]
    rank = {a["account_id"]: i for i, a in enumerate(accounts(user, run))} if run else {}      # accounts() is already highest priority first
    out.sort(key=lambda d: (d["status"] != "pending", rank.get(d["account_id"], 10 ** 6), d.get("kind") != "csm_note"))
    return meta, out


def change_lines(user, run):
    try:
        if not run or not table_exists("agent.change"):
            return []
        return _change_lines(user, run)
    except DBError as ex:
        sys.stderr.write(f"agent.change unavailable: {ex}\n")
        return []


def _change_lines(user, run):
    mine = {a["account_id"]: a["account_name"] for a in accounts(user, run)}
    rows = query("""select to_jsonb(c)::text as r from agent.change c where c.tenant_id = %(t)s
                    and c.run_id = (select max(run_id) from agent.change where tenant_id = %(t)s)""", {"t": user["tenant_id"]}, user["tenant_id"])
    out = []
    for r in rows:
        d = json.loads(r["r"])
        if d.get("account_id") not in mine:
            continue
        text = describe_change(d)
        if text:
            out.append((d.get("kind") != "entered_red", f"<a href='/account/{pages.e(d['account_id'], quote=True)}'>{pages.e(mine[d['account_id']])}</a> {pages.e(text)}"))
    return [t for _, t in sorted(out, key=lambda x: x[0])][:15]


def describe_change(d: dict) -> str | None:
    """Plain-language line for one row of agent.change (kinds are defined by the agent's check constraint)."""
    k, det = d.get("kind"), j(d.get("detail"), {}) if not isinstance(d.get("detail"), dict) else d["detail"]
    pct = lambda v: f"{round(float(v) * 100)} in 100" if v is not None else "?"
    if k == "entered_red":
        return f"moved into 'Call this week' (now {pct(d.get('to_risk'))})"
    if k == "left_red":
        return f"is no longer red (now {d.get('to_band') or 'lower'})"
    if k == "new_alert":
        return "has a new alert: " + ", ".join(str(x).replace("_", " ") for x in (det or {}).get("kinds", []))
    if k == "risk_jump":
        return f"risk rose by {round(abs(float((det or {}).get('delta', 0))) * 100)} points since last week"
    if k == "csm_departed":
        return "lost its CSM; ask a manager to reassign it"
    return None


# --------------------------------------------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "AccountHealth"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, fmt % args if args else fmt))

    # --- plumbing
    def send(self, code, body, headers=None, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for k, v in (headers or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def cookie(self, value, age):
        return f"session={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}" + ("; Secure" if SECURE else "")

    def current_user(self):
        raw = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)session=([^;]+)", raw)
        name = auth.read_session(m.group(1) if m else None)
        if not name:
            return None
        rows = query("select username, tenant_id, role, owner_id, display_name from ui.app_user where username = %(u)s", {"u": name})
        return rows[0] if rows else None

    def form(self):
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}

    def dispatch(self, method):
        try:
            path = urlparse(self.path).path
            if path == "/healthz":
                return self.send(200, json.dumps({"ok": True}), ctype="application/json")
            if path == "/login":
                return self.login(method)
            user = self.current_user()
            if not user:
                return self.redirect("/login")
            csrf = auth.csrf_token(user["username"])
            if method == "POST":
                f = self.form()
                if not auth.csrf_ok(user["username"], f.get("csrf")):
                    return self.send(403, pages.message(user, "Request not accepted", "The form expired. Go back, reload the page, and try again.", csrf))
                if path == "/logout":
                    return self.redirect("/login", self.cookie("", 0))
                m = re.fullmatch(r"/queue/([^/]+)", path)
                if m:
                    return self.decide(user, unquote(m.group(1)), f, csrf)
                return self.send(404, pages.message(user, "Page not found", "There is nothing at this address.", csrf))
            return self.page(user, path, csrf)
        except DBError as ex:
            sys.stderr.write(f"db error: {ex}\n")
            self.send(500, pages.message(None, "Something went wrong", "We could not reach the data. Try again in a minute."))
        except Exception as ex:                      # never show internals to the browser
            sys.stderr.write(f"error: {ex!r}\n")
            self.send(500, pages.message(None, "Something went wrong", "Try again in a minute."))

    do_GET = lambda self: self.dispatch("GET")
    do_POST = lambda self: self.dispatch("POST")

    # --- pages
    def login(self, method):
        if method == "GET":
            hint = ("<p class='when'>Demo logins: csm.lumen, manager.lumen, csm.parcel, manager.parcel. Ask the reviewer notes for the password.</p>" if SHOW_HINT else "")
            return self.send(200, pages.login(hint=hint))
        f = self.form()
        name, ip = (f.get("username") or "").strip().lower(), self.client_address[0]
        recent = [t for t in ATTEMPTS[(ip, name)] if t > time.time() - 300]
        ATTEMPTS[(ip, name)] = recent
        if len(recent) >= 10:
            return self.send(429, pages.login("Too many attempts. Wait five minutes and try again."))
        rows = query("select username, pw_hash from ui.app_user where username = %(u)s", {"u": name})
        ok = auth.verify_password(f.get("password") or "", rows[0]["pw_hash"] if rows else None)
        if not ok:
            ATTEMPTS[(ip, name)].append(time.time())
            return self.send(401, pages.login("That username and password do not match."))
        self.redirect("/", self.cookie(auth.make_session(rows[0]["username"]), auth.SESSION_SECONDS))

    def page(self, user, path, csrf):
        run = latest_run(user["tenant_id"])
        if path == "/":
            if user["role"] == "manager":
                return self.redirect("/team")
            return self.send(200, pages.week(user, run, accounts(user, run) if run else [], change_lines(user, run), csrf))
        if path == "/queue":
            meta, ds = drafts(user, run)
            return self.send(200, pages.queue(user, ds, meta is not None, csrf, parse_qs(urlparse(self.path).query).get("done", [""])[0][:80]))
        if path == "/team":
            if user["role"] != "manager":
                return self.send(403, pages.message(user, "Managers only", "The team view is for managers.", csrf))
            if not run:
                return self.send(200, pages.team(user, None, [], [], csrf))
            csms = query("""select owner_id, csm_name, csm_active, count(*) as n, count(*) filter (where band = 'red') as red,
                              count(*) filter (where band = 'yellow') as yellow, count(*) filter (where band = 'green') as green
                            from score.account_score where run_id = %(r)s and tenant_id = %(t)s group by 1, 2, 3 order by red desc, n desc""",
                         {"r": run["run_id"], "t": user["tenant_id"]}, user["tenant_id"])
            orphans = [a for a in accounts(user, run) if not a["csm_active"]]
            return self.send(200, pages.team(user, run, csms, orphans, csrf))
        m = re.fullmatch(r"/account/([A-Za-z0-9_-]{1,40})", path)
        if m and run:
            found = accounts(user, run, m.group(1))
            if found:
                a = found[0]
                hist = query("""select r.asof::text as asof, a.risk from score.account_score a join score.run r using (run_id)
                                where a.tenant_id = %(t)s and a.account_id = %(a)s order by r.asof desc limit 12""",
                             {"t": user["tenant_id"], "a": a["account_id"]}, user["tenant_id"])
                pts = [(h["asof"], float(h["risk"])) for h in reversed(hist)]
                _, ds = drafts(user, run, account_id=a["account_id"])
                live = [d for d in ds or [] if d["status"] in ("pending", "approved") and not d["superseded"]]
                return self.send(200, pages.account(user, run, a, pts, live, csrf))
        return self.send(404, pages.message(user, "Page not found", "That account does not exist, or it is not one of yours.", csrf))

    def decide(self, user, draft_id, f, csrf):
        run = latest_run(user["tenant_id"])
        meta, ds = drafts(user, run)
        d = next((x for x in ds if x["id"] == draft_id), None)          # only drafts this user is allowed to see
        action = f.get("action")
        if not meta or not d or d["status"] != "pending" or action not in ("approve", "reject"):
            return self.send(404, pages.message(user, "Draft not found", "It may already be decided, or it is not yours.", csrf))
        sets, p = ["status = %(st)s"], {"st": "approved" if action == "approve" else "rejected", "t": user["tenant_id"], "id": draft_id}
        if "decided_by" in meta["cols"]:
            sets.append("decided_by = %(u)s")
            p["u"] = user["username"]
        if "decided_at" in meta["cols"]:
            sets.append("decided_at = now()")
        body = (f.get("body") or "").strip()
        if body and body != (d["edited_body"] or d["body"] or "").strip() and "edited_body" in meta["cols"]:
            sets.append("edited_body = %(b)s")
            p["b"] = body[:20000]
        query(f"update agent.draft set {', '.join(sets)} where tenant_id = %(t)s and {meta['id']}::text = %(id)s and status = 'pending'", p, user["tenant_id"])
        self.redirect("/queue?done=" + quote("Approved. Copy it from below to send." if action == "approve" else "Rejected."))


def make_server(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    a = ap.parse_args(argv)
    srv = make_server(a.port, a.host)
    print(f"serving on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
