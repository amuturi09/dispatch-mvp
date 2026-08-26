# Quickstart — test the app

Three tracks, from "click around now" to "real phone calls." Start at Track 1.

---

## Track 1 — Contractor portal, locally (no external accounts) ⏱️ ~2 min

Everything runs against a local SQLite database. Telephony/billing are stubbed,
but the full contractor experience — sign up, onboarding wizard, dashboard,
on-call toggle, coverage, pricing, leads, billing view — works for real.

```bash
# 1. install dependencies (once), into a virtualenv
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

# 2. minimal config
cp .env.example .env         # the defaults in it are fine for local testing

# 3. (optional) load a demo contractor + sample leads so the dashboard has data
.venv\Scripts\python seed_demo.py

# 4. run
.venv\Scripts\python -m uvicorn main:app --reload
```

Then open **http://localhost:8000/partner**

- **New contractor:** click *Create account* and go through the 6-step setup.
- **Prefilled demo (if you ran step 3):** *Sign in* with
  `demo@rapiddispatch.test` / `demo12345` — the dashboard loads with 8 sample
  leads and billing history.

Useful URLs while testing:
| URL | What it is |
|---|---|
| `/partner` | the contractor portal |
| `/docs` | interactive API docs (Swagger) for every endpoint |
| `/healthz` | which providers are configured |

**What you need from your side for Track 1:** nothing. It runs offline.

---

## Track 2 — Persist to your Supabase database ⏱️ ~5 min

Same app, real database. Only one thing changes: `DATABASE_URL`.

1. In Supabase: **Project Settings → Database → Connection string → URI**. Copy it.
2. In your `.env`, set:
   ```
   DATABASE_URL=postgresql://postgres:<PASSWORD>@db.<PROJECT-REF>.supabase.co:5432/postgres
   SESSION_SECRET=<paste a long random string>
   ```
   Generate a secret: `python -c "import secrets; print(secrets.token_hex(32))"`
3. Restart the server. Tables are created automatically on startup. Re-run
   `python seed_demo.py` if you want the demo data in Supabase too.

The browser never talks to Supabase directly — it calls the API, and the API
talks to the database. Nothing in the portal changes.

**What you need from your side for Track 2:** your Supabase project's connection
string (and its database password). Paste it into `.env` — it never leaves your
machine.

---

## Track 3 — Real phone calls + card billing (later)

This adds Twilio (phone line), Retell (voice agent), and Stripe (card on file +
lead charges). It needs public HTTPS (ngrok locally) and accounts with each
provider. Full walkthrough: **`CREDENTIALS_SETUP.md`**.

**What you need from your side for Track 3:** Twilio / Retell / Stripe accounts
and API keys, plus an ngrok URL. Fill the remaining sections of `.env`.

---

## Admin / analytics (separate surface)

The operator analytics live behind admin auth, not in the contractor portal.
Set `ADMIN_AUTH_TOKEN` in `.env`, then:

```bash
curl -H "Authorization: Bearer <ADMIN_AUTH_TOKEN>" http://localhost:8000/api/v1/admin/analytics
```

Contractors have no route to this data — see `tests/test_partner_scoping.py`.

---

## Run the tests

```bash
.venv\Scripts\python -m pytest -q
```
