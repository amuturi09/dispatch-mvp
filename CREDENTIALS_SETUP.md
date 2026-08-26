# Credentials Setup

Every value below goes in `.env` (copy from `.env.example`). Nothing in the
codebase reads credentials any other way — `config.py` is the single source
of truth and will refuse to start in production if anything's missing or
dangerously misconfigured (e.g. a live Stripe key outside `APP_ENV=production`).

## 1. Twilio (telephony)

1. Create an account at twilio.com, verify it.
2. Console → **Account → API keys & tokens** → copy:
   - `TWILIO_ACCOUNT_SID` (starts with `AC`)
   - `TWILIO_AUTH_TOKEN` — treat this like a password; it's also the HMAC
     secret used to verify inbound webhooks are really from Twilio
     (`integrations/twilio_telephony.py::verify_twilio_signature`).
3. Buy a number: Console → **Phone Numbers → Buy a Number** (toll-free or
   local). This becomes `TWILIO_FROM_NUMBER`.
4. Under that number's **Voice Configuration**, set "A call comes in" to
   webhook → `https://<PUBLIC_BASE_URL>/webhooks/twilio/inbound` (you'll add
   this route once you build the inbound-call handler — not included in this
   pass, which focused on matching/billing; see "What's still not built" below).
5. For local testing before you have a real domain, use `ngrok http 8000` and
   put the ngrok HTTPS URL in `PUBLIC_BASE_URL`.

## 2. Stripe (billing)

1. Create an account at stripe.com. Stay in **test mode** (toggle top-right)
   until you're genuinely ready to charge real contractors.
2. Developers → **API keys** → copy the **Secret key** → `STRIPE_SECRET_KEY`
   (starts `sk_test_...`, later `sk_live_...`).
3. Developers → **Webhooks → Add endpoint**:
   - URL: `https://<PUBLIC_BASE_URL>/webhooks/stripe`
   - Events to send: `checkout.session.completed` (required — this is how a
     contractor's card-on-file gets activated) and optionally
     `payment_intent.payment_failed` for monitoring.
   - Copy the **Signing secret** shown after creation → `STRIPE_WEBHOOK_SIGNING_SECRET`.
4. For local testing without a public URL yet: install the Stripe CLI and run
   `stripe listen --forward-to localhost:8000/webhooks/stripe` — it prints a
   temporary signing secret you can use locally.
5. Going live later: switch the dashboard to live mode, generate new live
   keys, create a *second* webhook endpoint in live mode (test/live webhook
   secrets are different), and only then set `APP_ENV=production` +
   `sk_live_...` together — `config.py` will refuse any other combination.

## 3. Retell AI (voice orchestration)

1. Sign up at retellai.com, create an agent.
2. Dashboard → **API Keys** → generate a key **with the "webhook" badge** —
   this exact key is required; a non-webhook key will fail signature
   verification (Retell uses your API key itself as the HMAC secret, not a
   separate webhook secret). → `RETELL_API_KEY`.
3. In your agent's config, add a **Custom Function / Tool** named
   `match_and_transfer_contractor` pointing at
   `https://<PUBLIC_BASE_URL>/api/v1/dispatch/match`, with the parameter
   schema from `voice_agent_prompt.md`.
4. Paste the system prompt from `voice_agent_prompt.md` into the agent config.
5. (Optional) `RETELL_AGENT_ID` — only needed if you manage agent config
   programmatically via their API rather than the dashboard.

## 4. Database

- Local dev: no setup needed, `DATABASE_URL` defaults to a SQLite file.
- Production: provision Postgres (Railway, Supabase, RDS, etc.) and set
  `DATABASE_URL=postgresql://user:pass@host:5432/dbname`. SQLite has no real
  concurrent-writer story — don't run it in production once you have live
  traffic from multiple webhook sources hitting the app at once.

## 5. Public URL

- Every provider above needs to reach your server over HTTPS. Options:
  - Deploy to Railway/Render/Fly.io and use the URL they give you.
  - For local development, `ngrok http 8000` and use the printed HTTPS URL.
- Set this as `PUBLIC_BASE_URL`. In production, `config.py` refuses to start
  if this isn't `https://`.

## 6. Auth boundary (admin vs. contractor)

Two independent surfaces, two independent secrets. This is what keeps
contractors out of the operator's marketplace analytics.

- `ADMIN_AUTH_TOKEN` — a long random string (e.g. `openssl rand -hex 32`).
  Gates the **operator/admin** endpoints: contractor onboarding/listing and
  `GET /api/v1/admin/analytics` (network-wide leads, revenue, conversion).
  Send it as `Authorization: Bearer <token>`. If unset, admin endpoints run
  **unprotected in dev** (a warning is logged) — set it before going public.
- `SESSION_SECRET` — a long random string used to sign **contractor** session
  tokens (`partner_auth.py`). Contractors sign up / log in at
  `/api/v1/partner/*` and receive a token scoped to their own account; every
  partner route returns only that contractor's own data. **Required in
  production** — `config.py` refuses to start without it; in dev it falls back
  to a fixed insecure key so local login works. Rotating this value invalidates
  all outstanding contractor sessions.

The separation is enforced server-side, not by hiding UI: a contractor token
never resolves to another contractor, and no partner route exposes any
network aggregate (see `tests/test_partner_scoping.py`).

## Order of operations for a first real end-to-end test

1. Fill in `.env` with test-mode Stripe, real Twilio, real Retell.
2. `uvicorn main:app --reload`, confirm `GET /healthz` shows all three
   providers configured.
3. Run `python -m scripts.onboard_contractor` for one test contractor,
   complete the Stripe Checkout link yourself (test card `4242 4242 4242 4242`).
4. Confirm `GET /api/v1/contractors` shows `billing_mandate: true`.
5. Call your Twilio number, walk through the voice agent, confirm
   `/api/v1/dispatch/match` fires and returns a whisper script.
6. Only after that works end-to-end in test mode should you touch a live
   Stripe key.

## What's still not built (flagged, not hidden)

- SMS/TCPA consent capture for contractors at sign-up.
- Contractor session **revocation / password reset**. Tokens are stateless and
  expire (12h default); there's no server-side "log out everywhere" yet beyond
  rotating `SESSION_SECRET`, and no password-reset email flow.
- Rate limiting on `/api/v1/partner/login` and `/signup` (add before public
  launch to blunt credential-stuffing).

Note: admin auth **is** now wired — `ADMIN_AUTH_TOKEN` gates the onboarding,
listing, and analytics endpoints (see section 6). It's only unprotected if you
leave the token unset in dev.
