"""HTML for every page. Plain functions: data in, string out. Everything user- or data-derived goes through e() (escaped)."""
from __future__ import annotations

import html
from datetime import date, datetime

e = html.escape

BAND_WORDS = {"red": "Call this week", "yellow": "Keep an eye on", "green": "On track"}
KIND_WORDS = {"csm_note": "Note to you", "customer_email": "Email to the customer", "email": "Email to the customer"}

CSS = """
:root{--ink:#1b2333;--ink2:#586277;--paper:#f3f5f9;--surface:#fff;--line:#dce1ea;--red:#b3302f;--red-bg:#fbeaea;--amber:#8f5b00;--amber-bg:#fcf0d6;
--green:#26704f;--green-bg:#e3f2ea;--link:#2750c0;--focus:#2750c0;--serif:Charter,'Iowan Old Style','Palatino Linotype',Georgia,serif;
--sans:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
:root[data-theme=dark]{--ink:#e6eaf3;--ink2:#9aa5ba;--paper:#0f131b;--surface:#171c27;--line:#2a3144;--red:#ff8b86;--red-bg:#3a1c1e;--amber:#f0b955;
--amber-bg:#352a12;--green:#6cc79d;--green-bg:#16301f;--link:#8fabff;--focus:#8fabff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--ink:#e6eaf3;--ink2:#9aa5ba;--paper:#0f131b;--surface:#171c27;--line:#2a3144;--red:#ff8b86;
--red-bg:#3a1c1e;--amber:#f0b955;--amber-bg:#352a12;--green:#6cc79d;--green-bg:#16301f;--link:#8fabff;--focus:#8fabff}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
a{color:var(--link)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:3px}
.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;background:var(--surface);padding:8px;z-index:9}
header.top{background:var(--surface);border-bottom:1px solid var(--line)}
header.top .in{max-width:64rem;margin:0 auto;padding:12px 16px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.brand{font:700 1.05rem var(--serif);letter-spacing:.01em;margin-right:auto;text-decoration:none;color:var(--ink)}
nav a{color:var(--ink2);text-decoration:none;padding:6px 2px;margin-right:14px;border-bottom:2px solid transparent}
nav a[aria-current=page]{color:var(--ink);border-bottom-color:var(--ink)}
.who{color:var(--ink2);font-size:.9rem}.who form{display:inline;margin-left:6px}
main{max-width:64rem;margin:0 auto;padding:24px 16px 64px}
h1{font:600 1.9rem/1.2 var(--serif);margin:.2em 0 .1em}
h2{font:600 1.2rem/1.3 var(--serif);margin:1.8em 0 .5em}
.sub{color:var(--ink2);margin:0 0 1.4em}
button,.btn{font:inherit;border:1px solid var(--ink);background:var(--ink);color:var(--paper);padding:7px 14px;border-radius:6px;cursor:pointer}
button.quiet{background:transparent;color:var(--ink);border-color:var(--line)}
button:hover{opacity:.88}
.group>summary,.group>h2{cursor:default}
.group>summary{cursor:pointer;font:600 1.2rem var(--serif);margin:1.6em 0 .5em}
.row{display:grid;grid-template-columns:1fr 9.5rem 7rem;gap:6px 18px;align-items:center;background:var(--surface);
 border:1px solid var(--line);border-left:5px solid var(--bandc);border-radius:6px;padding:12px 14px;margin:0 0 8px}
.row.red{--bandc:var(--red)}.row.yellow{--bandc:var(--amber)}.row.green{--bandc:var(--green)}
.row .name{font-weight:650;color:var(--ink);text-decoration:none}.row .name:hover{text-decoration:underline}
.row .why{color:var(--ink2);font-size:.92rem;margin-top:2px}
.when{font-size:.85rem;color:var(--ink2)}
.runway{display:block;position:relative;height:6px;background:var(--line);border-radius:3px;margin-top:6px}
.runway i{position:absolute;top:-4px;left:var(--at);width:14px;height:14px;margin-left:-7px;border-radius:50%;background:var(--bandc);border:2px solid var(--surface)}
.runway::before{content:"";position:absolute;left:0;top:0;bottom:0;width:var(--at);background:var(--bandc);opacity:.35;border-radius:3px}
.risk{text-align:right;font-family:var(--serif)}.risk b{font-size:1.5rem}.risk span{display:block;font:.8rem var(--sans);color:var(--ink2)}
.chip{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.85rem;font-weight:600}
.chip.red{background:var(--red-bg);color:var(--red)}.chip.yellow{background:var(--amber-bg);color:var(--amber)}.chip.green{background:var(--green-bg);color:var(--green)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 18px;margin:0 0 12px}
.cols{display:grid;grid-template-columns:2fr 1fr;gap:24px;margin-bottom:28px}
.reason{margin:0 0 12px}.reason small{display:block;color:var(--ink2)}
.alert{border-left:4px solid var(--red);padding-left:12px}
ul.plain{list-style:none;margin:0;padding:0}ul.plain li{margin:0 0 10px}
dl.facts{margin:0}dl.facts dt{color:var(--ink2);font-size:.85rem;margin-top:10px}dl.facts dd{margin:0;font-family:var(--serif);font-size:1.15rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:.93rem}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.note{background:var(--amber-bg);color:var(--ink);border-radius:6px;padding:10px 14px;margin:0 0 10px}
.empty{border:1px dashed var(--line);border-radius:6px;padding:18px;color:var(--ink2)}
textarea{width:100%;min-height:7rem;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--ink)}
.draft>summary{cursor:pointer}.draft[open]>summary{margin-bottom:8px}.draft pre{white-space:pre-wrap;font:inherit;margin:0}
.done{color:var(--green);font-weight:600}.error{color:var(--red)}
form.login{max-width:22rem;margin:8vh auto}form.login label{display:block;margin:12px 0 4px}
form.login input{width:100%;padding:9px;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink)}
.spark{width:100%;height:56px}
@media (max-width:640px){.row{grid-template-columns:1fr 6.5rem}.row .track{grid-column:1/2}.cols{grid-template-columns:1fr}h1{font-size:1.55rem}}
@media print{header.top,button{display:none}}
"""


def fmt_date(s) -> str:
    try:
        d = datetime.fromisoformat(str(s)[:10])
        return d.strftime("%d %b %Y").lstrip("0")
    except Exception:
        return str(s or "")


def weeks_phrase(w) -> str:
    try:
        w = float(w)
    except (TypeError, ValueError):
        return ""
    return "renews this week" if w < 1 else f"renews in {round(w)} week{'s' if round(w) != 1 else ''}"


def in100(p) -> str:
    return str(int(round(float(p) * 100)))


def layout(title: str, body: str, user: dict | None = None, active: str = "", csrf: str = "") -> str:
    nav = ""
    if user:
        links = [("/", "This week", "week"), ("/queue", "Drafts to approve", "queue")]
        if user["role"] == "manager":
            links = [("/team", "Team", "team"), ("/queue", "Drafts to approve", "queue")]
        nav = "<nav aria-label='Main'>" + "".join(
            f"<a href='{h}'{' aria-current=\"page\"' if k == active else ''}>{e(t)}</a>" for h, t, k in links) + "</nav>"
        nav += (f"<span class='who'>{e(user['display_name'])} <form method='post' action='/logout'>"
                f"<input type='hidden' name='csrf' value='{e(csrf)}'><button class='quiet' type='submit'>Sign out</button></form></span>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><a class='skip' href='#main'>Skip to content</a>"
            f"<header class='top'><div class='in'><a class='brand' href='/'>Account health</a>{nav}</div></header>"
            f"<main id='main'>{body}</main></body></html>")


def login(error: str = "", hint: str = "") -> str:
    err = f"<p class='error' role='alert'>{e(error)}</p>" if error else ""
    return layout("Sign in", f"""<form class='login' method='post' action='/login'><h1>Sign in</h1>
      <p class='sub'>Your weekly list of accounts that need you.</p>{err}
      <label for='u'>Username</label><input id='u' name='username' autocomplete='username' required autofocus>
      <label for='p'>Password</label><input id='p' name='password' type='password' autocomplete='current-password' required>
      <p><button type='submit'>Sign in</button></p>{hint}</form>""")


def _top_reason(r: dict) -> str:
    if r["alerts"]:
        return r["alerts"][0]["text"]
    if real(r["drivers"]):
        return real(r["drivers"])[0]["text"]
    if r["data_note"] == "no_data_in_any_source":
        return "No product, support or email data yet; score reflects the account profile only"
    return "No single factor stands out"


def real(items):
    """Drop reasons that carry no sentence (the scorer falls back to the bare concept name for concepts without a template)."""
    return [x for x in items or [] if x.get("text") and x["text"] != x.get("concept")]


def row(r: dict) -> str:
    at = max(0.0, min(1.0, float(r["weeks_to_renewal"] or 0) / 26.0)) * 100
    flags = ""
    if not r["csm_active"]:
        flags += " <span class='chip yellow'>CSM left</span>"
    if r["renewal_source"] == "projected":
        flags += " <span class='when'>(date projected)</span>"
    return (f"<div class='row {e(r['band'])}'><div><a class='name' href='/account/{e(r['account_id'], quote=True)}'>{e(r['account_name'])}</a>{flags}"
            f"<div class='why'>{e(_top_reason(r))}</div></div>"
            f"<div class='track'><div class='when'>{e(weeks_phrase(r['weeks_to_renewal']))}</div>"
            f"<span class='runway' style='--at:{at:.1f}%' role='img' aria-label='{e(weeks_phrase(r['weeks_to_renewal']))}, on a 26 week scale'><i></i></span></div>"
            f"<div class='risk'><b>{in100(r['priority'])}</b><span>in 100</span></div></div>")


def week(user: dict, run: dict | None, accts: list[dict], changes: list[dict], csrf: str) -> str:
    if not run:
        return layout("This week", "<h1>This week</h1><div class='empty'>No scores yet. The first weekly run has not happened.</div>", user, "week", csrf)
    n = {b: [a for a in accts if a["band"] == b] for b in ("red", "yellow", "green")}
    body = [f"<h1>Your week</h1><p class='sub'>{len(accts)} accounts renew in the next 26 weeks. Scores as of {e(fmt_date(run['asof']))}. "
            f"The number on the right is how many of 100 similar renewals ended in churn or a big downgrade.</p>"]
    if changes:
        body.append("<h2>What changed since last week</h2><div class='panel'><ul class='plain'>" + "".join(
            f"<li>{c}</li>" for c in changes) + "</ul></div>")
    if not accts:
        body.append("<div class='empty'>Nothing to show: no accounts of yours renew in the next 26 weeks.</div>")
    for b in ("red", "yellow", "green"):
        rows = "".join(row(a) for a in n[b])
        head = f"{BAND_WORDS[b]} ({len(n[b])})"
        if not n[b]:
            continue
        if b == "green":
            body.append(f"<details class='group'><summary>{e(head)}</summary>{rows}</details>")
        else:
            body.append(f"<section class='group'><h2>{e(head)}</h2>{rows}</section>")
    return layout("This week", "".join(body), user, "week", csrf)


def sparkline(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return "<p class='when'>The trend line appears after a second weekly run.</p>"
    w, h, pad = 220, 56, 6
    xs = [pad + i * (w - 2 * pad) / (len(points) - 1) for i in range(len(points))]
    hi = max(0.5, max(p for _, p in points))
    ys = [h - pad - (p / hi) * (h - 2 * pad) for _, p in points]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    return (f"<svg class='spark' viewBox='0 0 {w} {h}' role='img' aria-label='Risk over the last {len(points)} weekly runs, now {in100(points[-1][1])} in 100'>"
            f"<polyline points='{pts}' fill='none' stroke='currentColor' stroke-width='2'/><circle cx='{xs[-1]:.1f}' cy='{ys[-1]:.1f}' r='4' fill='currentColor'/></svg>")


def draft_card(d: dict, csrf: str, show_account: bool = True, can_decide: bool = True, open_: bool = True) -> str:
    st = d["status"]
    kind = KIND_WORDS.get(d.get("kind") or "", d.get("kind") or "Draft")
    link = (f"<a href='/account/{e(d['account_id'], quote=True)}'>{e(d.get('account_name') or d['account_id'])}</a>"
            if show_account and d.get("account_id") else "")
    reasons = f"<p class='when'>Why this draft: {e(d['reasons'])}</p>" if d.get("reasons") else ""
    to = f"<p class='when'>To: {e(d['to'])}</p>" if d.get("to") else ""
    subj = f"<p><b>{e(d['subject'])}</b></p>" if d.get("subject") else ""
    shown = d.get("edited_body") or d.get("body") or ""
    if st == "pending" and can_decide:
        form = (f"<form method='post' action='/queue/{e(d['id'], quote=True)}'><input type='hidden' name='csrf' value='{e(csrf)}'>"
                f"<label class='when' for='b{e(d['id'])}'>Edit before approving</label>"
                f"<textarea id='b{e(d['id'])}' name='body'>{e(shown)}</textarea>"
                f"<p><button name='action' value='approve' type='submit'>Approve</button> "
                f"<button class='quiet' name='action' value='reject' type='submit'>Reject</button></p></form>")
        state = ""
    elif st == "approved":
        state = (f"<p class='done'>Approved &ndash; copy to send. Nothing is sent from here.</p><textarea readonly aria-label='Approved text'>{e(shown)}</textarea>")
        form = ""
    else:
        state, form = f"<p class='when'>{e(st.title())}</p>", f"<div class='draft'><pre>{e(shown)}</pre></div>"
    name = e(d.get("account_name") or d.get("account_id") or "") if show_account else ""
    summary = f"<summary><b>{name}</b> {'&middot; ' if name else ''}{e(kind)}{' &ndash; ' + e(d['subject']) if d.get('subject') else ''}</summary>"
    return (f"<details class='panel draft'{' open' if open_ else ''}>{summary}<p class='when'>{link}</p>{subj}{to}{reasons}{state}{form}</details>")


def queue(user: dict, drafts: list[dict], available: bool, csrf: str, msg: str = "") -> str:
    body = ["<h1>Drafts to approve</h1><p class='sub'>The system drafts; you decide. This tool never sends email: approve a draft, then copy it into your own mail.</p>"]
    if msg:
        body.append(f"<p class='done' role='status'>{e(msg)}</p>")
    if not available:
        body.append("<div class='empty'>No drafts yet. Drafts appear here after the first Monday run.</div>")
    else:
        stale = [d for d in drafts if d.get("superseded")]
        drafts = [d for d in drafts if not d.get("superseded")]
        pend = [d for d in drafts if d["status"] == "pending"]
        done = [d for d in drafts if d["status"] != "pending"]
        if not drafts:
            body.append("<div class='empty'>No drafts yet. Drafts appear here after the Monday run.</div>")
        if pend:
            body.append(f"<h2>Waiting for you ({len(pend)})</h2><p class='when'>Highest-risk accounts first. Open a draft to read, edit and decide.</p>"
                        + "".join(draft_card(d, csrf, open_=i < 2) for i, d in enumerate(pend)))
        if stale:
            body.append(f"<p class='when'>{len(stale)} older draft{'s' if len(stale) != 1 else ''} for the same accounts "
                        f"{'are' if len(stale) != 1 else 'is'} hidden because a newer one replaced {'them' if len(stale) != 1 else 'it'}.</p>")
        if done:
            body.append("<h2>Decided</h2>" + "".join(draft_card(d, csrf, can_decide=False, open_=False) for d in done[:20]))
    return layout("Drafts to approve", "".join(body), user, "queue", csrf)


def account(user: dict, run: dict, a: dict, history: list, drafts: list[dict], csrf: str) -> str:
    drivers, prot, alerts, feats = real(a["drivers"]), real(a["protective"]), a["alerts"], a["features"]
    lifted = float(a["priority"]) > float(a["risk"]) + 1e-9
    head = (f"<p><a href='/'>&larr; Back to your week</a></p><h1>{e(a['account_name'])}</h1>"
            f"<p><span class='chip {e(a['band'])}'>{e(BAND_WORDS[a['band']])}</span> &nbsp;{e(weeks_phrase(a['weeks_to_renewal']))} "
            f"({e(fmt_date(a['renewal_date']))})</p>")
    notes = []
    if not a["csm_active"]:
        notes.append("Its CSM has left the company. Ask a manager to reassign this account.")
    if a["renewal_source"] == "projected":
        notes.append("The renewal date is projected from the last anniversary. No renewal is in the CRM yet.")
    if a["data_note"] == "no_data_in_any_source":
        notes.append("We have no product, support or email data for this account. The score reflects its profile only. Quiet is not the same as healthy or unhealthy.")
    elif a["data_note"] == "silent_for_90_days":
        notes.append("Nothing from this account in any source for 90 days. That can mean trouble or just a quiet customer; a call settles it.")
    if feats.get("has_email") in (0, 0.0) and a["data_note"] != "no_data_in_any_source":
        notes.append("No email from your mailbox is linked to this account, so email signals are missing.")
    why = ""
    for al in alerts:
        why += (f"<div class='reason alert'><b>{e(al['text'])}</b><small>{e(al.get('evidence', ''))}</small></div>")
    for dr in drivers:
        why += f"<div class='reason'>{e(dr['text'])}</div>"
    if not why:
        why = "<p>No single factor stands out. The score comes from many small signals.</p>"
    favour = "".join(f"<div class='reason'>{e(p['text'])}</div>" for p in prot) or "<p class='when'>Nothing stands out in its favour.</p>"
    if lifted:
        risk_line = (f"The statistical score alone is {in100(a['risk'])} in 100. The alert above lifts this account to about {in100(a['priority'])} in 100 "
                     f"(the measured rate for accounts like this) and shows it as {a['band']}.")
    else:
        risk_line = f"About {in100(a['risk'])} in 100 renewals like this ended in churn or a big downgrade."
    facts = (f"<dl class='facts'><dt>Account owner</dt><dd>{e(a['csm_name'] or 'Unassigned')}</dd><dt>Segment</dt><dd>{e(a['segment'] or '')}</dd>"
             f"<dt>Statistical score</dt><dd>{in100(a['risk'])} in 100</dd>"
             + (f"<dt>Priority with alert</dt><dd>{in100(a['priority'])} in 100</dd>" if lifted else "") + "</dl>")
    dr_html = "".join(draft_card(d, csrf, show_account=False) for d in drafts) or "<p class='when'>No drafts for this account yet.</p>"
    rows = "".join(f"<tr><td>{e(k)}</td><td class='n'>{e('' if v is None else (f'{v:.3f}'.rstrip('0').rstrip('.') if isinstance(v, float) else str(v)))}</td></tr>"
                   for k, v in sorted(feats.items()))
    body = (head + "".join(f"<div class='note'>{e(n)}</div>" for n in notes) + f"<p>{e(risk_line)}</p><div class='cols'><div>"
            f"<h2>Why it is flagged</h2><div class='panel'>{why}</div><h2>In its favour</h2><div class='panel'>{favour}</div>"
            f"<h2>Drafts</h2>{dr_html}</div><div><h2>Facts</h2><div class='panel'>{facts}</div><h2>Risk over time</h2>"
            f"<div class='panel'>{sparkline(history)}</div></div></div>"
            f"<details class='panel'><summary>How this was computed</summary><p class='when'>Run {e(run['run_id'])} &middot; model {e(run['model_sha'])} "
            f"(trained on renewals before {e(run['trained_through'])}) &middot; scored {e(fmt_date(run['asof']))}. Only facts the warehouse knew by then are used. "
            f"These are the exact numbers the model saw.</p><table><thead><tr><th>Feature</th><th class='n'>Value</th></tr></thead><tbody>{rows}</tbody></table></details>")
    return layout(a["account_name"], body, user, "week", csrf)


def team(user: dict, run: dict | None, csms: list[dict], orphans: list[dict], csrf: str) -> str:
    if not run:
        return layout("Team", "<h1>Team</h1><div class='empty'>No scores yet.</div>", user, "team", csrf)
    trs = "".join(f"<tr><td>{e(c['csm_name'] or 'Unassigned')}{'' if c['csm_active'] == 't' else ' <span class=\"chip yellow\">left</span>'}</td>"
                  f"<td class='n'>{e(c['n'])}</td><td class='n'>{e(c['red'])}</td><td class='n'>{e(c['yellow'])}</td><td class='n'>{e(c['green'])}</td></tr>" for c in csms)
    orows = "".join(row(a) for a in orphans)
    body = (f"<h1>Team</h1><p class='sub'>Accounts renewing in the next 26 weeks, by CSM. Scores as of {e(fmt_date(run['asof']))}.</p>"
            f"<table><thead><tr><th>CSM</th><th class='n'>Accounts</th><th class='n'>Call this week</th><th class='n'>Keep an eye on</th><th class='n'>On track</th></tr></thead><tbody>{trs}</tbody></table>"
            f"<h2>Accounts with no CSM ({len(orphans)})</h2>"
            + (orows if orphans else "<div class='empty'>Every account renewing soon has an active CSM.</div>"))
    return layout("Team", body, user, "team", csrf)


def message(user: dict | None, title: str, text: str, csrf: str = "") -> str:
    return layout(title, f"<h1>{e(title)}</h1><p>{e(text)}</p><p><a href='/'>Back to the start</a></p>", user, "", csrf)
