# Complete Testing Guide: Dispatch Agent End-to-End

This guide walks you through testing the complete agent system end-to-end, from first call to successful billing.

## Prerequisites

You have:
- A Retell AI account with an agent that has the `match_and_transfer_contractor` function
- A Twilio account with a real phone number (not trial)
- A Stripe account in test mode
- Python 3.8+
- ngrok installed
- A way to make test phone calls (your phone works fine)

## Part 1: Setup (30 minutes)

### 1.1 Clone and Configure

```bash
cd dispatch_mvp
pip install -r requirements.txt
cp .env.example .env
```

### 1.2 Fill .env with Test Credentials

```
APP_ENV=development
PUBLIC_BASE_URL=https://your-ngrok-url.ngrok.io    # will fill this in next step

TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM_NUMBER=+18335550100    # your Twilio phone number

STRIPE_SECRET_KEY=sk_test_xxxxx    # from Stripe > Developers > API keys
STRIPE_WEBHOOK_SIGNING_SECRET=whsec_test_xxxxx    # (leave blank for now, will fill after stripe listen starts)

RETELL_API_KEY=key_xxxxx    # must have "webhook" badge
RETELL_AGENT_ID=agent_xxxxx
RETELL_SIP_DOMAIN=abc123.sip.livekit.cloud    # from Retell dashboard > Custom Telephony

ADMIN_AUTH_TOKEN=test_token_12345    # any secret string

DATABASE_URL=sqlite:///./dispatch_mvp.db
ALLOW_TEST_MODE_BILLING=true
```

### 1.3 Start Three Terminals

**Terminal 1: ngrok**
```bash
ngrok http 8000
# Output will show:
# Forwarding https://abc123.ngrok.io -> http://localhost:8000
# Copy the HTTPS URL to .env as PUBLIC_BASE_URL
```

**Terminal 2: Stripe webhook listener**
```bash
stripe listen --forward-to http://localhost:8000/webhooks/stripe
# Output will show:
# > Ready! Your webhook signing secret is: whsec_test_xxxxxxx
# Copy this to .env as STRIPE_WEBHOOK_SIGNING_SECRET
```

**Terminal 3: FastAPI server**
```bash
pip install -r requirements.txt
# After filling .env and getting ngrok + stripe signing secret:
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
# Should start without errors; check http://localhost:8000/healthz
```

Update .env with the ngrok URL and Stripe signing secret, then restart the server.

### 1.4 Verify Setup

```bash
# Check all three providers are configured
curl http://localhost:8000/healthz

# Should return:
{
  "status": "ok",
  "env": "development",
  "stripe_configured": true,
  "stripe_live": false,
  "twilio_configured": true,
  "retell_configured": true,
  "database": "sqlite",
  "admin_auth_enabled": true
}
```

## Part 2: Contractor Setup (10 minutes)

### 2.1 Onboard a Test Contractor

```bash
curl -X POST http://localhost:8000/api/v1/contractors/onboard \
  -H "Authorization: Bearer test_token_12345" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "test_contractor_1",
    "name": "Test Plumber Inc",
    "phone_number": "+1234567890",
    "trade": "plumbing",
    "coverage_zips": ["77001", "77002", "77003", "77004", "77005"],
    "base_bid": 65.0,
    "reputation_score": 4.8
  }'

# Response will include checkout_url like:
# "checkout_url": "https://checkout.stripe.com/pay/cs_test_..."
```

### 2.2 Complete Contractor Card Setup

1. Visit the checkout_url
2. Fill in:
   - Email: test@example.com
   - Card: `4242 4242 4242 4242`
   - Exp: `12/25`
   - CVC: `123`
   - Name: "Test Contractor"
3. Click Pay
4. You'll be redirected; that's fine
5. Check server logs — you should see:
   ```
   Contractor test_contractor_1 completed card setup -- eligible for billable leads.
   ```

### 2.3 Verify Contractor is Billing-Eligible

```bash
curl http://localhost:8000/api/v1/contractors \
  -H "Authorization: Bearer test_token_12345"

# Should show:
# [
#   {
#     "id": "test_contractor_1",
#     "name": "Test Plumber Inc",
#     "billing_mandate": true,
#     ...
#   }
# ]
```

## Part 3: Make a Test Call (15 minutes)

### 3.1 Prepare

1. Have your phone ready (you'll be the caller)
2. Have another phone/softphone ready, or use your computer's speaker (you'll be the contractor)
3. Watch the server logs during the call
4. Have a pen ready to jot down the lead ID from logs

### 3.2 Call Flow

**Step 1: Call your Twilio number**

Dial: `+18335550100` (replace with your actual Twilio number)

**Step 2: Listen to opening message**

You should hear:
> "Thanks for calling Dialpatch. We're connecting you with an AI agent to help with your emergency. Please stand by."

Then Retell's agent should greet you:
> "Thanks for calling Dialpatch. This is an automated line that connects you with an independent local contractor for a service fee — this is not 911..."

**Step 3: Triage with agent**

Agent will ask:
1. "What service do you need?" → Say: "Plumbing"
2. "What's your ZIP code?" → Say: "77002"
3. "Can you give me your street address?" → Say: "123 Main Street"
4. "How urgent is this?" → Say: "Emergency" or "Critical"

**Step 4: Watch the matching happen**

In your server logs, you should see:
```
POST /api/v1/dispatch/match
Matched: Test Plumber Inc
Lead ID: xxxxx-xxxxx-xxxxx-xxxxx
Lead fee: $71.75 (or similar, depends on surge pricing)
```

Then you'll see:
```
Dialing contractor test_contractor_1...
Contractor SMS sent to +1234567890
```

**Step 5: Answer as contractor**

Your contractor phone rings. Answer it.

You'll hear a whisper:
> "Emergency plumbing lead in 77002. Urgency: critical. Lead fee: $71.75. Press 1 to connect."

**Step 6: Accept or decline**

- **To accept**: Press `1`
- **To decline**: Press `2` (or any key except 1)

### 3.3 If You Accepted (Pressed 1)

Both legs bridge into a conference. You should now be able to hear yourself (the homeowner) and can have a conversation.

The caller will hear:
> "Thanks — we're connecting you with a local contractor now. Please stay on the line."

Then silence (hold music, which we didn't set up, so they'll just hear the contractor).

**Simulate conversation** for ~90 seconds to exceed the 60-second billing threshold.

Then hang up.

**In your logs**, you should see:
```
Contractor-complete webhook fired
Duration: 90+ seconds
Billing settlement triggered
[TEST MODE] Would charge $71.75 to contractor test_contractor_1
Lead marked as billed
```

### 3.4 If You Declined (Pressed 2)

The caller will stay on hold and hear:
> "Thanks — we're connecting you with a local contractor now. Please stay on the line."

**In your logs**, you should see failover:
```
Contractor test_contractor_1 declined lead xxxxx
Failover triggered
No more contractors available (you only onboarded one)
[Caller hears] "Unfortunately, we don't have any more available contractors..."
```

## Part 4: Validate Full Flow (5 minutes)

### 4.1 Check Database

```bash
sqlite3 dispatch_mvp.db
SELECT * FROM leads WHERE id = 'xxxxx-xxxxx-xxxxx-xxxxx';
```

Expected columns:
- `status`: `billed` (if call ≥60 seconds) or `matched` / `no_match` / `flagged_safety`
- `contractor_id`: `test_contractor_1`
- `lead_fee`: `71.75`
- `billed`: `1` (true) if duration ≥60s
- `call_duration_seconds`: `90+`
- `call_status`: `completed`

### 4.2 Check Stripe (in Stripe Dashboard)

**Developers → Events**

You should see:
- `checkout.session.completed` (when contractor finished card setup)

Look for test charges (won't exist if you stayed in test mode, but settlement would have fired).

## Part 5: Test Failover with Multiple Contractors (10 minutes, optional)

To test the failover logic, onboard a second contractor:

```bash
curl -X POST http://localhost:8000/api/v1/contractors/onboard \
  -H "Authorization: Bearer test_token_12345" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "test_contractor_2",
    "name": "Emergency Rooter Co",
    "phone_number": "+9876543210",
    "trade": "plumbing",
    "coverage_zips": ["77001", "77002"],
    "base_bid": 55.0
  }'

# Complete their Stripe checkout

# Now make another test call
# When the whisper plays to test_contractor_1:
# - If they press 2 (decline), test_contractor_2 will be dialed next
# - If test_contractor_2 presses 1, they bridge
# - If test_contractor_2 also declines, caller gets apology message
```

## Part 6: Safety Escalation (5 minutes, optional)

To test that safety keywords are caught:

When Retell's agent asks you questions, say something like:
> "We have a gas leak and a plumbing problem"

Expected behavior:
- Agent immediately says: "Please hang up and dial 911 right now."
- Call ends without matching or billing
- Server logs show: `Lead status: FLAGGED_SAFETY`

## Troubleshooting

### "Invalid Twilio signature"

- Make sure you're using the exact public HTTPS URL from ngrok
- Twilio signs the webhook based on the URL, so mismatches fail verification
- Check that `X-Twilio-Signature` header is present in requests

### "Stripe webhook not received"

- Make sure `stripe listen --forward-to http://localhost:8000/webhooks/stripe` is running
- After it prints the signing secret, put that in .env and restart the server
- Check Stripe CLI window for incoming events

### "Retell agent not working"

- Verify RETELL_AGENT_ID is correct (dashboard → Voice Agents → select agent → Agent ID at top)
- Verify the agent has the `match_and_transfer_contractor` function configured
- Check that the function's endpoint is set to `POST http://your-ngrok-url/api/v1/dispatch/match`

### "Call ends with 'technical difficulties'"

- Check server logs for the exact error
- Common causes:
  - Retell Register Phone Call API failed (verify SIP domain)
  - Twilio outbound call failed (verify from_number)
  - Database error (make sure migrations ran)

### "Contractor never rings"

- Verify contractor's phone number is correct and can receive calls
- Retell SIP dial timeout is 15 seconds per the code
- Server logs should show the outbound call being created

## What You Just Tested

✓ Contractor onboarding + Stripe card setup
✓ Inbound call receiving + Retell triage
✓ Dispatch matching + failover queue storage
✓ SMS notification to contractor
✓ Whisper prompt + DTMF keypress handling
✓ Conference bridging of both call legs
✓ Billing settlement (if duration ≥60s)
✓ Safety escalation (if you tested it)
✓ Failover logic (if you tested with 2 contractors)

## Next Steps: Production

Once you're confident with local testing:

1. **Get a real domain** (not ngrok)
2. **Set `APP_ENV=production`** in .env
3. **Get a live Stripe key** (`sk_live_...`)
4. **Deploy to Railway/Render/Fly** (one of these will give you a free tier)
5. **Update Twilio number's voice webhook** to point at your production domain
6. **Update Retell agent's function endpoint** to point at production domain
7. **Create Stripe webhook in live mode** (Dashboard → Webhooks → Add endpoint)
8. **Make a real test call** with a staging contractor
9. **Monitor first week** of live calls for any issues

See `CREDENTIALS_SETUP.md` for detailed production steps.
