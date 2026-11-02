"""Create the demo logins.   python -m ui.seed

Two tenants, one CSM and one manager each. The CSM chosen is the one with the most red accounts, so the demo has something to show.
Password comes from DEMO_PASSWORD (default below). It is demo data: change it before showing this to anyone who is not a reviewer.
"""
from __future__ import annotations

import os
from pathlib import Path

from .auth import hash_password
from .db import query

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "fello-demo-2026")
TENANTS = {"tnt_lumen": "lumen", "tnt_parcel": "parcel"}


def main() -> int:
    query(Path(__file__).with_name("schema.sql").read_text())
    run = query("select run_id from score.run order by asof desc limit 1")
    if not run:
        raise SystemExit("no score run yet: run `make score` first")
    rid = run[0]["run_id"]
    pw = hash_password(DEMO_PASSWORD)
    print(f"{'username':22s}{'tenant':12s}{'role':9s}name")
    for tenant, short in TENANTS.items():
        # the demo CSM is the one with the most pending drafts (so the queue is not empty), then the most red accounts
        pending = ("(select count(*) from agent.draft d where d.owner_id = s.owner_id and d.tenant_id = s.tenant_id and d.status = 'pending')"
                   if query("select (to_regclass('agent.draft') is not null) as t")[0]["t"] == "t" else "0")
        top = query(f"""select owner_id, csm_name, count(*) filter (where band = 'red') red, max({pending}) as drafts from score.account_score s
                       where run_id = %(r)s and tenant_id = %(t)s and csm_active group by 1, 2 order by drafts desc, red desc, csm_name limit 1""",
                    {"r": rid, "t": tenant}, tenant)[0]
        for username, role, owner, name in ((f"csm.{short}", "csm", top["owner_id"], top["csm_name"]),
                                            (f"manager.{short}", "manager", None, f"Manager ({short.title()})")):
            query("""insert into ui.app_user (username, tenant_id, role, owner_id, display_name, pw_hash)
                     values (%(u)s, %(t)s, %(ro)s, %(o)s, %(n)s, %(p)s)
                     on conflict (username) do update set tenant_id = excluded.tenant_id, role = excluded.role,
                       owner_id = excluded.owner_id, display_name = excluded.display_name, pw_hash = excluded.pw_hash""",
                  {"u": username, "t": tenant, "ro": role, "o": owner, "n": name, "p": pw}, tenant)
            print(f"{username:22s}{tenant:12s}{role:9s}{name}")
    query(Path(__file__).with_name("security.sql").read_text())
    print(f"\npassword for all four: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
