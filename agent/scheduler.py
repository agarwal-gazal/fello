"""Cron-friendly entry point.   python -m agent.scheduler --once

Computes the latest Monday on or before "now" and runs the agent for it ONLY if it has not already completed (agent.run status ok).
So it is safe to call every hour: all but the first call after Monday 06:00 UTC do nothing. A lock file stops two overlapping
invocations from running the same Monday twice. --catch-up N also runs any Monday in the last N weeks that never completed
(oldest first), which is how a server that was down over a weekend recovers on its own.

    # every hour, on the hour (no output unless it did something or failed)
    0 * * * * cd /srv/fello-health && DATABASE_URL=postgresql://... python -m agent.scheduler --once --catch-up 2 >> /var/log/fello-agent.log 2>&1

"Now" defaults to the dataset's export time (core.meta), because the data is a frozen export; in production pass nothing and it
uses the wall clock (--now wall).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys

import pandas as pd

from .db import ensure_schema, psql
from . import run as agent_run

LOCK = os.environ.get("FELLO_AGENT_LOCK", "/tmp/fello_agent.lock")


def latest_monday(now: pd.Timestamp) -> pd.Timestamp:
    if now.tzinfo is not None:
        now = now.tz_convert("UTC").tz_localize(None)
    d = now.normalize()
    monday = d - pd.Timedelta(days=d.weekday())
    if monday + pd.Timedelta(hours=agent_run.sc.RUN_HOUR) > now:         # Monday's run hour has not happened yet
        monday -= pd.Timedelta(days=7)
    return monday


def done(db_url: str, asof: pd.Timestamp) -> bool:
    return psql(db_url, f"select count(*) from agent.run where run_id = '{agent_run.run_id_for(asof)}' and status = 'ok'") == "1"


def resolve_now(db_url: str, now_arg: str) -> pd.Timestamp:
    if now_arg == "wall":
        return pd.Timestamp.now(tz="UTC").tz_localize(None)
    if now_arg:
        return pd.Timestamp(now_arg)
    return pd.Timestamp(psql(db_url, "select export_at from core.meta")).tz_convert("UTC").tz_localize(None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--once", action="store_true", required=True)
    ap.add_argument("--now", default=None, help="ISO time, or 'wall' for the wall clock (default: the dataset's export time)")
    ap.add_argument("--catch-up", type=int, default=0, help="also run missed Mondays from the previous N weeks")
    ap.add_argument("--model", default=agent_run.DEFAULT_MODEL)
    a = ap.parse_args(argv)
    if not a.database_url:
        sys.exit("set DATABASE_URL")
    with open(LOCK, "w") as fh:                              # held for the whole run, released when the process ends or this block exits
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another agent run is in progress; nothing to do")
            return 0
        return _locked(a)


def _locked(a) -> int:
    ensure_schema(a.database_url)
    latest = latest_monday(resolve_now(a.database_url, a.now))
    due = [latest - pd.Timedelta(weeks=i) for i in range(a.catch_up, -1, -1)]
    todo = [d for d in due if not done(a.database_url, d)]
    if not todo:
        print(f"{latest:%Y-%m-%d}: already done, nothing to do")
        return 0
    rc = 0
    for asof in todo:
        rc |= agent_run.main(["--database-url", a.database_url, "--asof", f"{asof:%Y-%m-%d}", "--model", a.model])
        if rc:
            break                                           # do not build later Mondays' diffs on top of a failed one
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
