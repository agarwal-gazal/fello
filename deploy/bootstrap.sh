#!/usr/bin/env bash
# Build the whole system inside an empty Postgres: synthetic data -> raw -> staging -> entity resolution -> core -> features ->
# model -> scores -> agent runs -> tenant isolation -> demo logins.
#
#   DATABASE_URL='postgresql://OWNER:PASSWORD@HOST/DB?sslmode=require' APP_DB_PASSWORD='long-random-string' bash deploy/bootstrap.sh
#
# Run it from a machine that has this repository, Python 3.11+ with `pip install -r deploy/requirements.txt`, and the `psql` command.
# The generated data stays on that machine (data/ is git-ignored); only the database is shared. Safe to re-run: every step is idempotent.
#   SCALE=0.6   smaller dataset (default 1 = 5,000 accounts, ~380 MB in the database)
#   SLIM=1      drop the raw and training-snapshot tables afterwards (saves ~210 MB for a 0.5 GB database; the web app and the weekly
#               agent do not need them, but you cannot rebuild from raw or retrain until you re-run this script)
#   DEMO_PASSWORD=...   password for the four demo logins (default fello-demo-2026: change it)
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DATABASE_URL:?set DATABASE_URL to the OWNER connection string}"
: "${APP_DB_PASSWORD:?set APP_DB_PASSWORD: the password for the restricted database role the web app uses}"
SCALE="${SCALE:-1}"; SEED="${SEED:-7}"; ASOF="${ASOF:-2026-10-26}"
step() { printf '\n== %s\n' "$*"; }

step "generate data (scale $SCALE, seed $SEED)"; python -m data_gen.generate --out data --seed "$SEED" --scale "$SCALE"
step "load raw";            python -m ingest.load_raw --data data
step "staging";             python -m ingest.build_staging
step "entity resolution";   python -m resolve.resolve
step "core layer";          python -m core.build_core --data data
step "features";            python -m features.build_features --data data
step "train + backtest";    python -m model.train
step "score as of $ASOF";   python -m scoring.score --asof "$ASOF"
step "tenant isolation (creates role fello_app, row-level security)"; python -m agent.security
if [ "$APP_DB_PASSWORD" = "fello" ]; then
  echo "WARNING: APP_DB_PASSWORD is the default 'fello'. Fine on your own laptop, never on a database anyone else can reach." >&2
else
  printf '%s\n' "alter role fello_app password :'pw';" | psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1 -v pw="$APP_DB_PASSWORD"
fi
step "agent: last four Mondays (what changed, draft notes and emails)"; python -m agent.scheduler --once --catch-up 3
step "demo logins";         python -m ui.seed
if [ "${SLIM:-0}" = "1" ]; then
  step "slim: dropping raw + training snapshots"
  psql "$DATABASE_URL" -X -q -c "drop schema raw cascade; drop schema feat cascade"
fi
echo
echo "done. The web app connects as:  postgresql://fello_app:<APP_DB_PASSWORD>@<same host>/<same db>?sslmode=require"
echo "commit model/artifacts/model.pkl for the weekly job:  git add -f model/artifacts/model.pkl"
