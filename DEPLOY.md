# Connect dialpatch.com (Railway + GoDaddy)

Goal: make the app reachable at **dialpatch.com** over HTTPS.

There are three parts. I can only do the code side; the two dashboard steps use
your Railway and GoDaddy accounts, so those are click-by-click for you below.

Recommended setup: **`www.dialpatch.com`** is the real address (Railway needs a
CNAME, and GoDaddy can't point the bare `dialpatch.com` at a CNAME). The bare
`dialpatch.com` then **forwards** to `www`. Typing either one lands on the app.

---

## Step 1 — Set environment variables in Railway

Railway → your project → the API service → **Variables**. Set:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `PUBLIC_BASE_URL` | `https://www.dialpatch.com` |
| `SESSION_SECRET` | a long random string — `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_AUTH_TOKEN` | another long random string (protects the analytics endpoints) |
| `DATABASE_URL` | your Supabase URI (Supabase → Project Settings → Database → Connection string → URI) |

Add Stripe/Twilio/Retell keys too when you're ready for real calls/billing
(see `CREDENTIALS_SETUP.md`). In `production`, the app refuses to start unless
the core values above are present and `PUBLIC_BASE_URL` is `https://`.

> Make sure the service exposes the right port. The app reads Railway's `$PORT`
> (`Procfile`: `uvicorn main:app --host 0.0.0.0 --port $PORT`).

---

## Step 2 — Add the domain in Railway

Railway → the API service → **Settings → Networking → Custom Domain**.

1. Enter **`www.dialpatch.com`** → Add.
2. Railway shows a **CNAME target** to copy — something like
   `abc123.up.railway.app`. **Copy that exact value** (use what Railway shows,
   not this example).

Leave this tab open; you'll paste that value into GoDaddy next.

---

## Step 3 — Point DNS at Railway (GoDaddy)

GoDaddy → **My Products → Domains → dialpatch.com → DNS / Manage DNS**.

**3a. CNAME for `www`:**
- **Add** a record → Type **CNAME**
- **Name:** `www`
- **Value:** the Railway target you copied (e.g. `abc123.up.railway.app`)
- **TTL:** default (1 hour) → **Save**
- If a default `www` CNAME (pointing to something like
  `dialpatch.com` or a parking page) already exists, **edit that one** instead
  of adding a duplicate.

**3b. Forward the bare domain to `www`:**
- GoDaddy → the domain → **Domain Settings → Forwarding → Add Forwarding**
- Forward **`dialpatch.com`** → **`https://www.dialpatch.com`**
- Type **Permanent (301)**, setting **Forward only** (no masking) → **Save**

That's the whole DNS change.

---

## Step 4 — Wait, then verify

- DNS + Railway's automatic HTTPS certificate usually finish within
  **5–30 minutes** (can be longer). Railway's Custom Domain row turns green
  when the cert is issued.
- Test:
  - `https://www.dialpatch.com/healthz` → JSON with `"status":"ok"`
  - `https://www.dialpatch.com/partner` → the contractor portal
  - `https://dialpatch.com` → should redirect to `www`

If the browser warns about the certificate, it's usually just not issued *yet* —
give it more time; don't change records repeatedly.

---

## Step 5 — Update provider webhooks (only for real calls/billing)

Once the domain resolves, point each provider at it (details in
`CREDENTIALS_SETUP.md`):

| Provider | Where | New URL |
|---|---|---|
| Twilio | your number → Voice → "A call comes in" | `https://www.dialpatch.com/webhooks/twilio/inbound` |
| Stripe | Developers → Webhooks → your endpoint | `https://www.dialpatch.com/webhooks/stripe` |
| Retell | agent → custom function `match_and_transfer_contractor` | `https://www.dialpatch.com/api/v1/dispatch/match` |

---

## Want the bare `dialpatch.com` as the real address (no `www`)?

GoDaddy can't CNAME the bare domain, so the clean way is to put **Cloudflare**
(free) in front: move the domain's nameservers to Cloudflare, add a **CNAME
`@` → Railway target** (Cloudflare flattens it at the apex), proxy on. Tell me
if you want that route and I'll write the Cloudflare steps — the `www` +
forwarding approach above is simpler and works today.
