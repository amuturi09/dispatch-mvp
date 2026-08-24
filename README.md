# Emergency Dispatch Exchange — MVP

A working backend + voice-agent spec for a paid contractor-referral marketplace
(plumbing/HVAC/locksmith) with a **testable, end-to-end inbound call flow**.

**Status:** Fully wired to real Twilio/Retell/Stripe credentials, with admin auth and a comprehensive testing guide.

## What's here

```
dispatch_mvp/
├── core/
│   ├── engine.py                    # matching, surge pricing, settlement logic — pure Python, no deps
│   └── test_flow.py                 # executable end-to-end simulation (7 scenarios, all passing)
├── db/
│   ├── models.py                    # SQLAlchemy: contractors, leads, call_sessions, idempotency
│   └── session.py                   # engine/session setup (SQLite locally, Postgres in production)
├── integrations/
│   ├── stripe_onboarding.py         # Checkout Session setup mode + verified billing webhooks
│   ├── twilio_telephony.py          # signature verification + TwiML for warm transfer + conference
│   ├── retell_calls.py              # Register Phone Call API wrapper (Retell custom-telephony)
│   └── retell_security.py           # X-Retell-Signature verification (tested, pure stdlib)
├── api/
│   └── main.py                      # FastAPI: inbound handlers, dispatch, settlement, admin routes
├── auth.py                          # Admin Bearer token auth for sensitive endpoints
├── config.py                        # single source of truth for all env vars; validates at startup
├── scripts/
│   └── onboard_contractor.py        # CLI to onboard a contractor + send Stripe setup link
├── .env.example
├── CREDENTIALS_SETUP.md             # exactly how to obtain every credential
├── TESTING_GUIDE.md                 # end-to-end testing with ngrok, Stripe CLI, real calls
├── voice_agent_prompt.md            # Retell system prompt
├── requirements.txt
└── README.md
```

## What's real vs. mocked right now

| Piece | Status |
|---|---|
| **Matching/ranking algorithm** | **Real, tested.** Run `python3 -m core.test_flow` — 7 scenarios pass. |
| **Surge pricing, billing logic** | **Real, tested.** Correct thresholds, duration gates, and settlement logic. |
| **Safety escalation guard** | **Real, tested** (defense-in-depth keyword check before matching). |
| **Consent gate** | **Real, tested** (`disclosure_acknowledged` must be true to match). |
| **Config validation** | **Real, tested** — blocks live Stripe keys outside production, requires HTTPS in production, etc. |
| **Retell signature verification** | **Real, tested** — valid/tampered/wrong-key/replay/malformed all correctly handled. |
| **Inbound call handler** | **Real, complete.** Receives Twilio webhook → registers with Retell's Register Phone Call API → dials Retell's SIP endpoint. Returns TwiML. |
| **Post-triage handler** | **Real, complete.** Retell SIP leg ends → checks if a lead matched → dials contractor or apologizes. |
| **Contractor whisper/bridge** | **Real, complete.** Whisper prompts contractor → DTMF keypress '1' bridges both legs into conference → failover if declined. |
| **Billing settlement** | **Real, complete.** Call ends → triggers settlement webhook → charges contractor's saved card off-session if duration ≥60s. |
| **Admin auth** | **Real.** Bearer token protection on contractor endpoints; skipped in dev if `ADMIN_AUTH_TOKEN` not set. |
| **Contractor onboarding** | **Real.** Creates Stripe customer → returns hosted Checkout Session → contractor completes setup → `has_valid_billing_mandate` flips true. |
| **Database persistence** | **Real.** SQLAlchemy models for contractors, leads, call sessions, and webhook idempotency ledger. SQLite by default (dev), swap for Postgres in production. |
| **Stripe integration** | **Real, untested from this sandbox** (no network access). Code uses current best practices: Checkout Session setup mode for card-on-file, verified webhooks, idempotency ledger. |
| **Twilio integration** | **Real, untested from this sandbox** (no network access). Code: signature verification, TwiML generation, call control via API. |

## Run the tested core logic right now

```bash
cd dispatch_mvp
python3 -m core.test_flow
```

## Getting Started: Local Testing

See **`TESTING_GUIDE.md`** for step-by-step instructions to test end-to-end locally using:
- ngrok (for HTTPS tunneling)
- Stripe CLI (for webhook simulation)
- Real test calls to your Twilio number
- A test contractor and test contractor phone number (can be your own!)

## Deploy the API

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in real credentials
uvicorn api.main:app --reload
```

**Full inbound call flow:**

1. **`POST /webhooks/twilio/inbound`** — homeowner calls Twilio number → register with Retell → dial Retell's SIP endpoint
2. **Retell agent runs triage** over SIP, then calls **`POST /api/v1/dispatch/match`** (signature-verified)
3. **`POST /webhooks/twilio/post-triage`** — Retell's leg ends → if matched, dial contractor → else apologize
4. **`POST /webhooks/twilio/whisper`** — play job details + wait for keypress → contractor presses '1' to accept
5. **`POST /webhooks/twilio/gather-bridge`** — handle DTMF, bridge both legs if '1' pressed
6. **`POST /webhooks/twilio/contractor-complete`** — call ends → settle billing (charge if duration ≥60s)

**Admin endpoints** (protected by `Authorization: Bearer <ADMIN_AUTH_TOKEN>`):
- `POST /api/v1/contractors/onboard` — onboard a new contractor
- `GET /api/v1/contractors` — list contractors

**Other endpoints:**
- `POST /webhooks/stripe` — verified Stripe webhook (contractor completes card setup)
- `GET /healthz` — confirms which providers are configured

See `CREDENTIALS_SETUP.md` for credential setup and `TESTING_GUIDE.md` for testing procedures.

## What's deliberately NOT built

This is a **paid contractor-referral marketplace**, not an emergency-response system.
It should never be positioned as a replacement for 911. If you pivot toward actual 
emergency services, that's a different (and far more heavily regulated) product.

## What you should do before going live

1. **Legal review.** "Emergency Dispatch" branding — even with the opening disclosure — may 
   imply emergency services. Discuss with a lawyer before spending on customer acquisition.
2. **Stripe compliance.** The current implementation uses SetupIntent + PaymentIntent 
   off-session charging. Stripe requires the contractor to be notified of each charge; 
   add email/SMS confirmation to your post-settlement webhook.
3. **TCPA/telemarketing law.** Outbound calls to contractors and SMS need documented consent 
   at sign-up and per-call. Have a lawyer review your onboarding flow.
4. **Contractor terms.** Define what happens if a contractor loses internet during a call, 
   or if they claim the homeowner didn't actually need the service. Include dispute terms 
   in your contractor agreement.
5. **Staging test.** Run through the full flow with a staging contractor and a test caller 
   before production launch. See `TESTING_GUIDE.md`.

## What's been tested

- Core matching/ranking logic (7 scenarios, all pass)
- Config validation (missing vars, dangerous combos)
- Retell signature verification (5 edge cases)
- End-to-end flow structure (inbound → triage → match → whisper → bridge → settlement)

What hasn't been tested (because this sandbox has no network):
- Live Twilio API calls
- Live Stripe charges
- Live Retell speech-to-text / LLM inference
- Live Retell Register Phone Call API
- Live bridging of actual audio
