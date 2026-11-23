# Putting it online: Neon (database) + Render (web app) + a weekly job

Nothing here has been deployed by me: I had no accounts and the build sandbox cannot reach those services. Every step below was
written from how the services work and checked only as far as it can be locally (see "What was and was not tested" at the end).
Budget about 45 minutes. Everything can stay on free plans except Render's cron job (use the GitHub Actions file instead).

You need: a GitHub account, a Neon account, a Render account (sign up with GitHub on both), and on your own computer this repository,
Python 3.11+, and the `psql` command (`brew install libpq` on a Mac, `sudo apt install postgresql-client` on Linux).

## 1. Put the repository on GitHub
1. GitHub → New repository → name it, keep it **private** for now (the graders can be invited later) → Create.
2. In the repository folder:
   ```
   git add -f model/artifacts/model.pkl      # 6 KB; the weekly job runs in a fresh container and needs the trained model
   git commit -m "Add trained model for deploy"
   git remote add origin https://github.com/YOU/REPO.git
   git push -u origin main
   ```
   `data/` stays out of git on purpose (it is generated; step 3 generates it).

## 2. Create the database on Neon
1. neon.tech → Sign up → **Create project**. Name: `account-health`. Postgres 16 or 17. Region: pick the one closest to your Render
   region (Singapore for both if you are in India).
2. On the project dashboard click **Connect**. Copy two strings:
   * **Direct connection** (turn the "Connection pooling" switch **off**). This is the *owner* URL. Use it in step 3 and for the weekly job.
   * You will build the app URL yourself in step 3 from the same host.
3. Size: the full dataset is about 380 MB in Postgres and Neon's free plan allows 0.5 GB. It fits with little room to spare. If it does not,
   run step 3 with `SLIM=1` (drops ~210 MB of raw tables the live system does not need) or `SCALE=0.6`.

## 3. Fill the database (from your computer, one time, 5 to 10 minutes)
```
python -m venv .venv && source .venv/bin/activate
pip install -r deploy/requirements.txt
export DATABASE_URL='postgresql://OWNER:PASSWORD@HOST/DBNAME?sslmode=require'    # the direct owner string from Neon
export APP_DB_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"; echo "$APP_DB_PASSWORD"   # SAVE this line
export DEMO_PASSWORD='choose-a-password-for-the-reviewers'
bash deploy/bootstrap.sh
```
It generates the data, loads and cleans it, trains the model, scores accounts, runs the agent for the last four Mondays, creates the
restricted database role `fello_app` with row-level security, and creates four demo logins. Re-running is safe.
The web app's database URL is the same string with the user and password swapped:
`postgresql://fello_app:APP_DB_PASSWORD@HOST/DBNAME?sslmode=require`.

## 4. Deploy the web app on Render
1. dashboard.render.com → **New → Blueprint** → connect GitHub → choose the repository.
   (Render looks for `render.yaml` in the repository root. Run `cp deploy/render.yaml render.yaml && git add render.yaml && git commit -m render && git push` first.)
2. Render shows the services in the file. For the web service it asks for **APP_DATABASE_URL**: paste the `fello_app` URL from step 3.
   Leave the owner URL out of the web service on purpose.
3. If you are on the free plan, delete the `monday-agent` cron block from the blueprint screen (it is a paid feature) and use step 5 instead.
4. **Apply**. The first build takes 3 to 6 minutes. When it says Live, open the URL. `…/healthz` must show `{"ok": true}`.
5. Sign in as `csm.lumen` with your `DEMO_PASSWORD`. Also try `csm.parcel`, `manager.lumen`, `manager.parcel`.
   Free web services sleep after 15 idle minutes: the first request after a nap takes about a minute. Warm it up before a demo.

## 5. Make the Monday agent run by itself
Pick one:
* **GitHub Actions (free).** `mkdir -p .github/workflows && cp deploy/weekly-agent.github-actions.yml .github/workflows/weekly-agent.yml`, commit and push.
  GitHub → repository → Settings → Secrets and variables → Actions → **New repository secret** → name `DATABASE_URL`, value = the **owner** string from step 2.
  Then Actions → weekly-agent → **Run workflow** once to prove it works. Mondays 01:00 UTC it runs on its own.
* **Render cron job (paid).** Keep the `monday-agent` block; set `DATABASE_URL` (owner string) when prompted.
The job runs `python -m agent.scheduler --once --now wall --catch-up 2`: it works out the latest Monday and does nothing if that Monday is
already done, so extra triggers are harmless. `--now wall` uses the real clock (the data itself is a frozen export, so a run after
the export date re-scores the same data as of a later Monday; say so in the walkthrough).

## 6. Check it before you send the link
- `/healthz` answers; unauthenticated pages redirect to the login.
- Sign in as `csm.lumen`, open an account, expand "How this was computed".
- While signed in as `csm.lumen`, edit the address to an account id from the Parcel tenant: it must say "not one of yours".
- Approve a draft: it shows "Approved – copy to send". Nothing is emailed; the app has no way to send mail.
- GitHub Actions or Render logs show an `agent-YYYYMMDD` line (or "already done").

## Swapping the database driver (what DB_DRIVER=psycopg does)
Locally the app talks to Postgres through the `psql` command (no packages needed; PyPI was blocked where this was built). Deployed, it uses
`psycopg` (`DB_DRIVER=psycopg`, set in the Dockerfile). The whole difference lives in `ui/db.py`: `_run_psql` and `_run_psycopg` both
implement `query(sql, params, tenant_id)` with the same return shape. Everything else in `ui/` is driver-agnostic. The psycopg path sets the
tenant with `set_config(..., true)` inside the transaction, so it stays correct behind a pooled connection. It keeps up to `DB_POOL_SIZE`
(default 8) idle connections for reuse instead of opening one per query, closes any connection that saw an error, and turns driver errors
into the same `DBError` the psql path raises. The full UI test suite runs on both drivers (`tests/test_ui.py`, `UITestsPsycopg`).

## What was and was not tested
Tested here: the web app (16 tests: login, signed cookies, tenant isolation, CSRF, approve/reject, escaping, no send path, and the whole
app through the restricted `fello_app` role with row-level security on); `deploy/bootstrap.sh` end to end into an empty database at 5% scale
(41 s); the app serving that bootstrapped database as `fello_app`.
**Not tested, because it cannot be from here:** the Docker build, Render, Neon, GitHub Actions, TLS, and the psycopg code path
(psycopg is not installable in the sandbox). If the first deploy fails, the likely culprits are: the owner string pasted where the app
string belongs, `?sslmode=require` missing, or Render's blueprint not finding `render.yaml` in the repository root.
The `fello_app` role is created with the password `fello` by `agent/security.py`; `bootstrap.sh` replaces it with `APP_DB_PASSWORD`
(the script warns if you keep the default). Demo logins are real logins on a public URL: choose a password you are happy to share with reviewers.
