"""
Emergency Dispatch Exchange -- FastAPI gateway with inbound call handling.

Full call flow:
1. /webhooks/twilio/inbound: Twilio receives call → register with Retell → dial SIP URI
2. Retell's agent handles triage over SIP
3. /webhooks/twilio/post-triage: SIP leg ends → check if a lead matched
4. If matched: dial contractor → /webhooks/twilio/whisper for keypress bridge
5. /webhooks/twilio/contractor-complete: Call ends → settle billing

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill in real values
    uvicorn main:app --reload
"""

from __future__ import annotations
import os
import sys
import uuid
import logging
import asyncio
from typing import Optional

sys.path.insert(0, '/app')

from fastapi import FastAPI, HTTPException, Request, Depends, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import load_config
from auth import AdminAuthValidator, make_admin_dependency
from partner_auth import (
    PartnerTokenService, hash_password, verify_password, bearer_token,
)
from db.session import make_engine, make_session_factory, init_db, get_db_dependency
from db.models import ContractorDB, LeadDB, CallSessionDB, WebhookEventDB
from core.engine import (
    Contractor, LeadRequest, DispatchEngine, Trade, UrgencyLevel, LeadStatus,
    CallSettlement, settle_call,
)
from integrations import stripe_onboarding
from integrations.twilio_telephony import (
    make_twilio_client, verify_twilio_signature, twiml_caller_hold, twiml_whisper_and_bridge,
    twiml_bridge_confirmed, twiml_decline_or_timeout, twiml_apology_and_hangup, conference_name_for_lead,
    twiml_safety_escalation, send_lead_notification_sms,
)
from integrations.retell_calls import register_phone_call, RetellApiError
from integrations.retell_security import verify_retell_signature, RetellVerificationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dispatch")

app = FastAPI(title="Emergency Dispatch Routing & Settlement Engine", version="0.3.0")

# ---------------------------------------------------------------------------
# Startup: load config, initialize DB, set up providers and auth.
# ---------------------------------------------------------------------------
_STRICT = os.getenv("APP_ENV") == "production"
try:
    cfg = load_config(require_all=_STRICT)
except Exception as e:
    if _STRICT:
        raise
    logger.warning(f"Starting in degraded mode -- {e}")
    cfg = load_config(require_all=False)

db_engine = make_engine(cfg.database_url)
SessionLocal = make_session_factory(db_engine)
init_db(db_engine)
get_db = get_db_dependency(SessionLocal)

if cfg.stripe.secret_key:
    stripe_onboarding.init_stripe(cfg.stripe)

_twilio_client = make_twilio_client(cfg.twilio) if cfg.twilio.account_sid else None

# Contractor (partner) session tokens. Distinct from admin auth: this gates the
# self-service portal, and every route that uses it is scoped to the caller's
# own contractor row -- a contractor can never read another contractor's data
# or any network-wide aggregate through these endpoints.
_partner_tokens = PartnerTokenService(cfg.session_secret)


def require_contractor(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> ContractorDB:
    """
    Resolve a partner Bearer token to the ContractorDB row it belongs to.
    Raises 401 on a missing/invalid/expired token or an unknown contractor.
    The returned row is the ONLY contractor a partner route may act on.
    """
    token = bearer_token(authorization)
    contractor_id = _partner_tokens.verify(token) if token else None
    if not contractor_id:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    contractor = db.query(ContractorDB).filter_by(id=contractor_id).first()
    if not contractor:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return contractor


_admin_auth = AdminAuthValidator(cfg.admin_auth_token)
if cfg.admin_auth_token:
    require_admin = make_admin_dependency(_admin_auth)
else:
    # Dev mode: no-op dependency if admin token not set.
    # Must stay a bare callable (like the make_admin_dependency branch above) --
    # the routes below already wrap it with Depends(require_admin). Wrapping it
    # here too produced Depends(Depends(...)), which FastAPI rejects at import.
    async def require_admin():
        logger.warning("Admin endpoints are unprotected (ADMIN_AUTH_TOKEN not set)")
        return None


def _load_engine_from_db(db: Session) -> DispatchEngine:
    rows = db.query(ContractorDB).all()
    contractors = [
        Contractor(
            id=r.id, name=r.name, phone_number=r.phone_number, trade=Trade(r.trade),
            coverage_zips=set(r.coverage_zips or []), is_active=r.is_active, base_bid=r.base_bid,
            stripe_customer_id=r.stripe_customer_id or "", reputation_score=r.reputation_score,
            has_valid_billing_mandate=r.has_valid_billing_mandate,
            consecutive_no_answers=r.consecutive_no_answers,
            max_consecutive_no_answers=r.max_consecutive_no_answers,
        )
        for r in rows
    ]
    return DispatchEngine(contractors)


def _require_provider(name: str, configured: bool):
    if not configured:
        raise HTTPException(status_code=503, detail=f"{name} is not configured on this deployment yet.")


def _send_contractor_sms(phone: str, lead_details: dict):
    """Background task: send SMS to contractor notifying them of incoming lead."""
    try:
        twilio_client = make_twilio_client(cfg.twilio)
        send_lead_notification_sms(twilio_client, cfg.twilio.from_number, phone, lead_details)
    except Exception as e:
        logger.error(f"Failed to send contractor SMS to {phone}: {e}")


async def _dial_next_contractor(lead: LeadDB, session: CallSessionDB, db: Session) -> tuple[bool, str]:
    """
    Attempts to dial the next contractor in the failover queue.
    Returns (success, twiml_response).
    """
    if not lead.failover_queue:
        return False, twiml_apology_and_hangup("Unfortunately, all available contractors in your area are unavailable.")
    
    next_contractor_id = lead.failover_queue.pop(0)
    next_contractor = db.query(ContractorDB).filter_by(id=next_contractor_id).first()
    
    if not next_contractor:
        # Recursively try next if this one was deleted
        db.commit()
        return await _dial_next_contractor(lead, session, db)
    
    twilio_client = make_twilio_client(cfg.twilio)
    try:
        contractor_call = twilio_client.calls.create(
            to=next_contractor.phone_number,
            from_=cfg.twilio.from_number,
            url=f"{cfg.base_url}/webhooks/twilio/whisper?lead_id={lead.id}",
            status_callback=f"{cfg.base_url}/webhooks/twilio/contractor-complete?lead_id={lead.id}",
            status_callback_event=["completed"],
            status_callback_method="POST",
            timeout=15,
        )
        session.contractor_call_sid = contractor_call.sid
        lead.contractor_id = next_contractor_id
        db.commit()
        
        # Send SMS to this contractor too
        asyncio.create_task(
            asyncio.to_thread(
                _send_contractor_sms,
                next_contractor.phone_number,
                {"trade": lead.trade, "zip_code": lead.zip_code, "urgency": lead.urgency, "lead_fee": lead.lead_fee}
            )
        )
        return True, ""  # empty string means "caller stays on hold"
    except Exception as e:
        logger.error(f"Failed to dial failover contractor {next_contractor_id}: {e}")
        db.commit()
        return await _dial_next_contractor(lead, session, db)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LeadRequestApi(BaseModel):
    caller_phone: str
    trade: Trade
    zip_code: str = Field(..., min_length=5, max_length=5)
    urgency: UrgencyLevel
    street_address: str
    disclosure_acknowledged: bool
    safety_flag_text: str = ""


class ContractorOnboardApi(BaseModel):
    id: str
    name: str
    phone_number: str
    trade: Trade
    coverage_zips: list[str]
    base_bid: float
    reputation_score: float = 4.0


# --- Partner (contractor self-service) schemas ---

class PartnerSignupApi(BaseModel):
    business_name: str = Field(..., min_length=1)
    owner_name: str = Field("", max_length=120)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    phone_number: str = Field(..., min_length=7)
    trade: Trade
    coverage_zips: list[str] = Field(default_factory=list)
    base_bid: float = Field(65.0, ge=0)
    sms_opt_in: bool = True


class PartnerLoginApi(BaseModel):
    email: str
    password: str


class PartnerProfileUpdateApi(BaseModel):
    """Every field optional -- a contractor edits only what they change. All
    changes apply to the authenticated contractor only. Email (the login
    identity) is intentionally not editable here."""
    business_name: Optional[str] = Field(None, min_length=1)
    owner_name: Optional[str] = None
    phone_number: Optional[str] = None
    trade: Optional[Trade] = None
    coverage_zips: Optional[list[str]] = None
    base_bid: Optional[float] = Field(None, ge=0)
    is_active: Optional[bool] = None  # the on-call switch
    sms_opt_in: Optional[bool] = None


# ---------------------------------------------------------------------------
# INBOUND CALL FLOW
# ---------------------------------------------------------------------------

@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_call(request: Request, db: Session = Depends(get_db)):
    """
    Twilio webhook for inbound calls. Immediately registers the call with
    Retell to get a SIP URI, returns TwiML that dials that URI.
    
    Expected Twilio config on your phone number's Voice Configuration:
      Webhook URL: POST https://<PUBLIC_BASE_URL>/webhooks/twilio/inbound
    """
    _require_provider("Twilio", bool(cfg.twilio.auth_token))
    _require_provider("Retell", bool(cfg.retell.api_key and cfg.retell.agent_id and cfg.retell.sip_domain))
    
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    full_url = str(request.url)

    if not verify_twilio_signature(cfg.twilio.auth_token, full_url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")

    twilio_call_sid = params.get("CallSid")
    caller_phone = params.get("From", "")

    try:
        registered = register_phone_call(
            api_key=cfg.retell.api_key,
            agent_id=cfg.retell.agent_id,
            from_number=caller_phone,
            to_number=params.get("To", cfg.twilio.from_number),
            sip_domain=cfg.retell.sip_domain,
            direction="inbound",
            metadata={"twilio_call_sid": twilio_call_sid},
        )
    except RetellApiError as e:
        logger.error(f"Failed to register call with Retell: {e}")
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We're experiencing technical difficulties. Please try again later.")
        vr.hangup()
        return str(vr)

    session = CallSessionDB(twilio_call_sid=twilio_call_sid, retell_call_id=registered.call_id, caller_phone=caller_phone)
    db.add(session)
    db.commit()

    from twilio.twiml.voice_response import VoiceResponse, Dial
    vr = VoiceResponse()
    vr.say("Thanks for calling Dialpatch. We're connecting you with an AI agent to help with your emergency. Please stand by.")
    dial = Dial(action=f"{cfg.base_url}/webhooks/twilio/post-triage?twilio_call_sid={twilio_call_sid}")
    dial.sip(registered.sip_uri)
    vr.append(dial)
    return str(vr)


@app.post("/webhooks/twilio/post-triage")
async def twilio_post_triage(request: Request, bg_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Fires once Retell's SIP leg (triage) ends. Checks if a lead was matched;
    if so, dials the contractor and sends them an SMS alert. If not, apologizes.
    """
    _require_provider("Twilio", bool(cfg.twilio.auth_token))
    
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    full_url = str(request.url)

    if not verify_twilio_signature(cfg.twilio.auth_token, full_url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")

    twilio_call_sid = request.query_params.get("twilio_call_sid")
    session = db.query(CallSessionDB).filter_by(twilio_call_sid=twilio_call_sid).first()
    
    if not session:
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We encountered an error. Goodbye.")
        vr.hangup()
        return str(vr)

    dial_status = params.get("DialCallStatus", "")
    if dial_status != "completed":
        logger.warning(f"SIP dial to Retell failed: {dial_status}")
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We're experiencing technical difficulties. Please try again later.")
        vr.hangup()
        return str(vr)

    lead = db.query(LeadDB).filter_by(id=session.lead_id).first() if session.lead_id else None
    
    if not lead or lead.status != LeadStatus.MATCHED.value:
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("Unfortunately, we don't have an available contractor for your area right now. We recommend searching online or calling 911 if this is urgent.")
        vr.hangup()
        return str(vr)

    contractor = db.query(ContractorDB).filter_by(id=lead.contractor_id).first()
    if not contractor:
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We're experiencing technical difficulties. Please try again later.")
        vr.hangup()
        return str(vr)

    twilio_client = make_twilio_client(cfg.twilio)
    try:
        contractor_call = twilio_client.calls.create(
            to=contractor.phone_number,
            from_=cfg.twilio.from_number,
            url=f"{cfg.base_url}/webhooks/twilio/whisper?lead_id={lead.id}",
            status_callback=f"{cfg.base_url}/webhooks/twilio/contractor-complete?lead_id={lead.id}",
            status_callback_event=["completed"],
            status_callback_method="POST",
            timeout=15,
        )
        session.contractor_call_sid = contractor_call.sid
        db.commit()
        
        # Send SMS notification to contractor
        bg_tasks.add_task(
            _send_contractor_sms,
            contractor.phone_number,
            {"trade": lead.trade, "zip_code": lead.zip_code, "urgency": lead.urgency, "lead_fee": lead.lead_fee}
        )
    except Exception as e:
        logger.error(f"Failed to dial contractor: {e}")
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We're connecting you now. Please stay on the line.")
        vr.hangup()
        return str(vr)

    return twiml_caller_hold(lead.id, hold_music_url="")


@app.post("/webhooks/twilio/whisper")
async def twilio_whisper(request: Request, db: Session = Depends(get_db)):
    """
    Played to the contractor once they answer. Whispers the job details and
    asks them to press 1 to accept. Returned as the TwiML URL for the
    outbound contractor call from /webhooks/twilio/post-triage.
    """
    _require_provider("Twilio", bool(cfg.twilio.auth_token))
    
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    full_url = str(request.url)

    if not verify_twilio_signature(cfg.twilio.auth_token, full_url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")

    lead_id = request.query_params.get("lead_id")
    lead = db.query(LeadDB).filter_by(id=lead_id).first()
    
    if not lead:
        return twiml_decline_or_timeout()

    return twiml_whisper_and_bridge(
        lead.id,
        lead.whisper_text or f"Emergency {lead.trade} lead in {lead.zip_code}. Urgency: {lead.urgency}. Lead fee: ${lead.lead_fee:.2f}. Press 1 to connect.",
        f"{cfg.base_url}/webhooks/twilio/gather-bridge?lead_id={lead_id}",
    )


@app.post("/webhooks/twilio/gather-bridge")
async def twilio_gather_bridge(request: Request, db: Session = Depends(get_db)):
    """
    Handles the contractor's DTMF keypress (1 = accept, anything else = decline).
    If accepted, bridges both legs into a conference.
    If declined, triggers failover to next contractor in queue.
    """
    _require_provider("Twilio", bool(cfg.twilio.auth_token))
    
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    full_url = str(request.url)

    if not verify_twilio_signature(cfg.twilio.auth_token, full_url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")

    lead_id = request.query_params.get("lead_id")
    digit_pressed = params.get("Digits", "")
    lead = db.query(LeadDB).filter_by(id=lead_id).first()
    
    if not lead:
        return twiml_decline_or_timeout()

    if digit_pressed == "1":
        # Contractor accepted - bridge
        lead.status = LeadStatus.BRIDGED.value
        db.commit()
        return twiml_bridge_confirmed(lead.id)
    
    # Contractor declined - try failover
    logger.info(f"Contractor {lead.contractor_id} declined lead {lead_id}. Attempting failover...")
    
    # Find the call session for this lead
    session = db.query(CallSessionDB).filter_by(lead_id=lead_id).first()
    if not session:
        return twiml_decline_or_timeout()
    
    if not lead.failover_queue:
        # No more contractors
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("Unfortunately, we don't have any more available contractors. We recommend searching online or calling 911 if this is urgent.")
        vr.hangup()
        return str(vr)
    
    next_contractor_id = lead.failover_queue.pop(0)
    next_contractor = db.query(ContractorDB).filter_by(id=next_contractor_id).first()
    
    if not next_contractor:
        db.commit()
        # Recursively try next if this one was deleted
        return twiml_decline_or_timeout()
    
    twilio_client = make_twilio_client(cfg.twilio)
    try:
        contractor_call = twilio_client.calls.create(
            to=next_contractor.phone_number,
            from_=cfg.twilio.from_number,
            url=f"{cfg.base_url}/webhooks/twilio/whisper?lead_id={lead.id}",
            status_callback=f"{cfg.base_url}/webhooks/twilio/contractor-complete?lead_id={lead.id}",
            status_callback_event=["completed"],
            status_callback_method="POST",
            timeout=15,
        )
        session.contractor_call_sid = contractor_call.sid
        lead.contractor_id = next_contractor_id
        db.commit()
        
        logger.info(f"Failover: dialing contractor {next_contractor_id} for lead {lead_id}")
        return twiml_caller_hold(lead.id, hold_music_url="")
    except Exception as e:
        logger.error(f"Failover failed to dial contractor {next_contractor_id}: {e}")
        db.commit()
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("We're having trouble reaching contractors. Please try again later.")
        vr.hangup()
        return str(vr)


@app.post("/webhooks/twilio/contractor-complete")
async def twilio_contractor_complete(request: Request, bg_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Fires when the contractor's call ends. Settles billing based on the
    combined duration of both the triage (Retell SIP) leg and the bridged
    conversation leg.
    
    Triggered by status_callback in /webhooks/twilio/post-triage.
    """
    _require_provider("Twilio", bool(cfg.twilio.auth_token))
    
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("x-twilio-signature", "")
    full_url = str(request.url)

    if not verify_twilio_signature(cfg.twilio.auth_token, full_url, params, signature):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")

    lead_id = request.query_params.get("lead_id")
    lead = db.query(LeadDB).filter_by(id=lead_id).first()
    
    if not lead or not lead.contractor_id:
        return {"status": "error", "reason": "Lead or contractor not found."}

    event_id = f"twilio:{params.get('CallSid')}:completed"
    if db.query(WebhookEventDB).filter_by(id=event_id).first():
        return {"status": "duplicate_ignored"}
    db.add(WebhookEventDB(id=event_id, provider="twilio"))

    duration = int(params.get("CallDuration", 0) or 0)
    settlement = CallSettlement(lead_id=lead.id, contractor_id=lead.contractor_id,
                                 call_duration_seconds=duration, call_status="completed")
    
    contractor = db.query(ContractorDB).filter_by(id=lead.contractor_id).first()

    def charge_fn(amount_cents: int):
        if not contractor or not contractor.has_valid_billing_mandate:
            logger.error(f"Refusing to charge contractor {lead.contractor_id}: no valid billing mandate.")
            return
        if cfg.stripe.is_live:
            bg_tasks.add_task(
                stripe_onboarding.charge_off_session,
                contractor.stripe_customer_id, contractor.stripe_payment_method_id,
                amount_cents, lead.id,
            )
        else:
            logger.info(f"[TEST MODE] Would charge ${amount_cents/100:.2f} to contractor {lead.contractor_id}")

    outcome = settle_call(settlement, lead.lead_fee or 0.0, charge_fn)
    lead.call_duration_seconds = duration
    lead.call_status = "completed"
    lead.billed = outcome.charged
    lead.billed_amount_cents = outcome.amount_cents
    if outcome.charged:
        lead.status = LeadStatus.BILLED.value
    db.commit()

    return {"status": "settlement_processed" if outcome.charged else "not_billed", "reason": outcome.reason}


# ---------------------------------------------------------------------------
# DISPATCH MATCHING (called by Retell's function/tool during triage)
# ---------------------------------------------------------------------------

@app.post("/api/v1/dispatch/match")
async def match_lead(request: Request, lead: LeadRequestApi, db: Session = Depends(get_db)):
    """Called by Retell's tool/function once triage slots are filled."""
    if cfg.retell.api_key:
        raw = await request.body()
        sig = request.headers.get("x-retell-signature", "")
        try:
            if not verify_retell_signature(raw, sig, cfg.retell.api_key):
                raise HTTPException(status_code=401, detail="Invalid Retell signature.")
        except RetellVerificationError as e:
            raise HTTPException(status_code=401, detail=str(e))

    live_engine = _load_engine_from_db(db)
    domain_lead = LeadRequest(
        caller_phone=lead.caller_phone, trade=lead.trade, zip_code=lead.zip_code,
        urgency=lead.urgency, street_address=lead.street_address,
        disclosure_acknowledged=lead.disclosure_acknowledged,
    )
    result = live_engine.match(domain_lead, safety_flag_text=lead.safety_flag_text)

    lead_row = LeadDB(
        id=result.lead_id, caller_phone=lead.caller_phone, trade=lead.trade.value,
        zip_code=lead.zip_code, urgency=lead.urgency.value, street_address=lead.street_address,
        status=result.status.value, disclosure_acknowledged=lead.disclosure_acknowledged,
        safety_flag_text=lead.safety_flag_text,
        contractor_id=result.contractor.id if result.contractor else None,
        lead_fee=result.lead_fee,
        whisper_text=result.whisper_message,
        failover_queue=[c.id for c in result.candidate_queue] if result.candidate_queue else [],
    )
    db.add(lead_row)

    session = db.query(CallSessionDB).filter(CallSessionDB.lead_id == None).first()
    if session:
        session.lead_id = result.lead_id
    
    db.commit()

    if result.status == LeadStatus.FLAGGED_SAFETY:
        return {"status": "safety_escalation", "message": "Please hang up and call 911 immediately."}
    if result.status == LeadStatus.NO_MATCH:
        raise HTTPException(status_code=404, detail=result.reason)

    return {
        "status": "match_found",
        "lead_id": result.lead_id,
        "contractor_name": result.contractor.name,
        "contractor_phone": result.contractor.phone_number,
        "whisper_message": result.whisper_message,
    }


# ---------------------------------------------------------------------------
# PARTNER PORTAL (contractor self-service)
#
# Boundary rule for this whole section: signup/login are public; every other
# route depends on require_contractor and touches ONLY that contractor's own
# row. No endpoint here returns another contractor's data or any network-wide
# aggregate -- those live under /api/v1/admin/* behind admin auth.
# ---------------------------------------------------------------------------

def _contractor_profile(c: ContractorDB) -> dict:
    """The contractor's own account view. Deliberately excludes anything about
    the wider marketplace. `stripe_payment_method_id` is an opaque Stripe token,
    never card data."""
    return {
        "id": c.id,
        "business_name": c.name,
        "owner_name": c.owner_name or "",
        "owner_email": c.email,
        "trade": c.trade,
        "phone_number": c.phone_number,
        "coverage_zips": c.coverage_zips or [],
        "base_bid": c.base_bid,
        "on_call": c.is_active,
        "sms_opt_in": bool(c.sms_opt_in),
        "reputation_score": c.reputation_score,
        "billing_active": c.has_valid_billing_mandate,
    }


@app.post("/api/v1/partner/signup")
async def partner_signup(body: PartnerSignupApi, db: Session = Depends(get_db)):
    """Public. Creates a contractor account with login credentials, and (if
    Stripe is configured) a hosted checkout link to add a card on file. Returns
    a session token so the new partner is signed in immediately."""
    email = body.email.strip().lower()
    if db.query(ContractorDB).filter_by(email=email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    contractor_id = f"ct_{uuid.uuid4().hex[:12]}"
    stripe_customer_id = None
    if cfg.stripe.secret_key:
        stripe_customer_id = stripe_onboarding.create_or_get_stripe_customer(
            contractor_id, body.business_name, body.phone_number
        )

    row = ContractorDB(
        id=contractor_id,
        name=body.business_name.strip(),
        phone_number=body.phone_number.strip(),
        trade=body.trade.value,
        coverage_zips=[z.strip() for z in body.coverage_zips if z.strip()],
        is_active=False,  # off-call until they finish setup and choose to go live
        base_bid=body.base_bid,
        email=email,
        owner_name=body.owner_name.strip() or None,
        password_hash=hash_password(body.password),
        sms_opt_in=body.sms_opt_in,
        stripe_customer_id=stripe_customer_id,
        has_valid_billing_mandate=False,
        reputation_score=4.0,
    )
    db.add(row)
    db.commit()

    checkout_url = None
    if cfg.stripe.secret_key:
        link = stripe_onboarding.create_onboarding_checkout_session(
            contractor_id=contractor_id,
            stripe_customer_id=stripe_customer_id,
            success_url=f"{cfg.base_url}/onboarding/success?contractor_id={contractor_id}",
            cancel_url=f"{cfg.base_url}/onboarding/cancelled?contractor_id={contractor_id}",
        )
        checkout_url = link.checkout_url

    return {
        "token": _partner_tokens.issue(contractor_id),
        "contractor": _contractor_profile(row),
        "checkout_url": checkout_url,
    }


@app.post("/api/v1/partner/login")
async def partner_login(body: PartnerLoginApi, db: Session = Depends(get_db)):
    """Public. Returns a session token on valid credentials."""
    email = body.email.strip().lower()
    contractor = db.query(ContractorDB).filter_by(email=email).first()
    # One generic error for unknown-email and wrong-password so the endpoint
    # can't be used to enumerate which emails have accounts.
    if not contractor or not verify_password(body.password, contractor.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return {
        "token": _partner_tokens.issue(contractor.id),
        "contractor": _contractor_profile(contractor),
    }


@app.get("/api/v1/partner/me")
async def partner_me(me: ContractorDB = Depends(require_contractor)):
    return _contractor_profile(me)


@app.patch("/api/v1/partner/me")
async def partner_update_me(
    body: PartnerProfileUpdateApi,
    me: ContractorDB = Depends(require_contractor),
    db: Session = Depends(get_db),
):
    """Update the caller's own profile / availability. Includes the on-call
    switch (`is_active`). Only ever mutates the authenticated contractor."""
    if body.business_name is not None:
        me.name = body.business_name.strip()
    if body.owner_name is not None:
        me.owner_name = body.owner_name.strip() or None
    if body.phone_number is not None:
        me.phone_number = body.phone_number.strip()
    if body.trade is not None:
        me.trade = body.trade.value
    if body.coverage_zips is not None:
        me.coverage_zips = [z.strip() for z in body.coverage_zips if z.strip()]
    if body.base_bid is not None:
        me.base_bid = body.base_bid
    if body.is_active is not None:
        me.is_active = body.is_active
    if body.sms_opt_in is not None:
        me.sms_opt_in = body.sms_opt_in
    db.commit()
    return _contractor_profile(me)


@app.get("/api/v1/partner/leads")
async def partner_leads(me: ContractorDB = Depends(require_contractor), db: Session = Depends(get_db)):
    """The caller's OWN lead history only -- the audit trail behind their bills."""
    rows = (
        db.query(LeadDB)
        .filter(LeadDB.contractor_id == me.id)
        .order_by(LeadDB.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": r.id,
            "trade": r.trade,
            "zip_code": r.zip_code,
            "urgency": r.urgency,
            "status": r.status,
            "lead_fee": r.lead_fee,
            "call_duration_seconds": r.call_duration_seconds,
            "billed": r.billed,
            "billed_amount_cents": r.billed_amount_cents,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.get("/api/v1/partner/billing")
async def partner_billing(me: ContractorDB = Depends(require_contractor), db: Session = Depends(get_db)):
    """The caller's own card-on-file status and charge history. No card data."""
    charges = (
        db.query(LeadDB)
        .filter(LeadDB.contractor_id == me.id, LeadDB.billed.is_(True))
        .order_by(LeadDB.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "billing_active": me.has_valid_billing_mandate,
        "has_payment_method": bool(me.stripe_payment_method_id),
        "total_charged_cents": sum(c.billed_amount_cents or 0 for c in charges),
        "charges": [
            {
                "lead_id": c.id,
                "zip_code": c.zip_code,
                "amount_cents": c.billed_amount_cents,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in charges
        ],
    }


@app.post("/api/v1/partner/billing/setup-link")
async def partner_billing_setup_link(
    me: ContractorDB = Depends(require_contractor), db: Session = Depends(get_db)
):
    """Fresh Stripe hosted-checkout link so the contractor can add or replace
    their card. Card details are entered on Stripe -- never sent to this API."""
    _require_provider("Stripe", bool(cfg.stripe.secret_key))
    if not me.stripe_customer_id:
        me.stripe_customer_id = stripe_onboarding.create_or_get_stripe_customer(me.id, me.name, me.phone_number)
        db.commit()
    link = stripe_onboarding.create_onboarding_checkout_session(
        contractor_id=me.id,
        stripe_customer_id=me.stripe_customer_id,
        success_url=f"{cfg.base_url}/onboarding/success?contractor_id={me.id}",
        cancel_url=f"{cfg.base_url}/onboarding/cancelled?contractor_id={me.id}",
    )
    return {"checkout_url": link.checkout_url}


# ---------------------------------------------------------------------------
# CONTRACTOR MANAGEMENT (protected by admin auth)
# ---------------------------------------------------------------------------

@app.post("/api/v1/contractors/onboard")
async def onboard_contractor(c: ContractorOnboardApi, db: Session = Depends(get_db), 
                              _admin=Depends(require_admin)):
    _require_provider("Stripe", bool(cfg.stripe.secret_key))

    if db.query(ContractorDB).filter_by(id=c.id).first():
        raise HTTPException(status_code=409, detail="Contractor ID already exists.")

    stripe_customer_id = stripe_onboarding.create_or_get_stripe_customer(c.id, c.name, c.phone_number)

    row = ContractorDB(
        id=c.id, name=c.name, phone_number=c.phone_number, trade=c.trade.value,
        coverage_zips=c.coverage_zips, is_active=True, base_bid=c.base_bid,
        stripe_customer_id=stripe_customer_id, reputation_score=c.reputation_score,
        has_valid_billing_mandate=False,
    )
    db.add(row)
    db.commit()

    link = stripe_onboarding.create_onboarding_checkout_session(
        contractor_id=c.id,
        stripe_customer_id=stripe_customer_id,
        success_url=f"{cfg.base_url}/onboarding/success?contractor_id={c.id}",
        cancel_url=f"{cfg.base_url}/onboarding/cancelled?contractor_id={c.id}",
    )
    return {
        "status": "pending_card_setup",
        "contractor_id": c.id,
        "checkout_url": link.checkout_url,
    }


@app.get("/api/v1/contractors")
async def list_contractors(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    rows = db.query(ContractorDB).all()
    return [
        {"id": r.id, "name": r.name, "trade": r.trade, "zips": r.coverage_zips,
         "active": r.is_active, "billing_mandate": r.has_valid_billing_mandate,
         "reputation": r.reputation_score}
        for r in rows
    ]


@app.get("/api/v1/admin/analytics")
async def admin_analytics(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Marketplace-wide analytics for the operator only. This is exactly the
    network-level data a contractor must never see -- it lives behind admin
    auth, and there is no partner route that returns any of it."""
    total_contractors = db.query(func.count(ContractorDB.id)).scalar() or 0
    on_call = db.query(func.count(ContractorDB.id)).filter(ContractorDB.is_active.is_(True)).scalar() or 0
    total_leads = db.query(func.count(LeadDB.id)).scalar() or 0
    billed_leads = db.query(func.count(LeadDB.id)).filter(LeadDB.billed.is_(True)).scalar() or 0
    gross_cents = db.query(func.coalesce(func.sum(LeadDB.billed_amount_cents), 0)).scalar() or 0
    by_status = dict(
        db.query(LeadDB.status, func.count(LeadDB.id)).group_by(LeadDB.status).all()
    )
    return {
        "contractors_total": total_contractors,
        "contractors_on_call": on_call,
        "leads_total": total_leads,
        "leads_billed": billed_leads,
        "conversion_rate": round(billed_leads / total_leads, 4) if total_leads else 0.0,
        "gross_revenue_cents": int(gross_cents),
        "leads_by_status": by_status,
    }


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    _require_provider("Stripe", bool(cfg.stripe.webhook_signing_secret))
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe_onboarding.verify_and_parse_webhook(payload, sig_header, cfg.stripe.webhook_signing_secret)
    except Exception as e:
        logger.warning(f"Rejected Stripe webhook: signature verification failed ({e})")
        raise HTTPException(status_code=400, detail="Invalid signature.")

    if db.query(WebhookEventDB).filter_by(id=event["id"]).first():
        return {"status": "duplicate_ignored"}

    # Handle the event and record it as seen in a SINGLE commit at the end.
    # The "seen" marker must be written only AFTER the side effects succeed:
    # if the handler fails partway (e.g. a transient error retrieving the
    # SetupIntent), nothing commits, so Stripe's automatic retry re-processes
    # the event. The previous order recorded the event first, so a failed first
    # attempt left the marker behind and every retry was skipped as a duplicate
    # -- silently swallowing the card-setup update.
    if event["type"] == "checkout.session.completed":
        session_id = event["data"]["object"]["id"]
        resolved = stripe_onboarding.resolve_completed_setup(session_id)
        contractor = db.query(ContractorDB).filter_by(id=resolved["contractor_id"]).first()
        if contractor and resolved["setup_intent_status"] == "succeeded":
            contractor.stripe_payment_method_id = resolved["payment_method_id"]
            contractor.has_valid_billing_mandate = True
            logger.info(f"Contractor {contractor.id} completed card setup.")

    db.add(WebhookEventDB(id=event["id"], provider="stripe"))
    db.commit()
    return {"status": "processed"}


# ---------------------------------------------------------------------------
# PARTNER PORTAL (served same-origin so the page can call /api/v1/partner/*)
#
# Only the contractor portal is served here. The admin/analytics console is a
# separate surface and is not exposed from this app.
# ---------------------------------------------------------------------------

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.get("/partner", include_in_schema=False)
async def partner_portal():
    return FileResponse(os.path.join(_WEB_DIR, "partner.html"))


def _stripe_return_page(title: str, message: str, tone: str) -> HTMLResponse:
    color = "#35d99a" if tone == "ok" else "#ffab3d"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#080b14;
color:#eaeff9;font-family:system-ui,-apple-system,'Segoe UI',sans-serif}}
.c{{max-width:380px;text-align:center;padding:32px}}
.d{{width:56px;height:56px;border-radius:50%;margin:0 auto 18px;display:grid;place-items:center;
background:rgba(53,217,154,.12);color:{color};font-size:28px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#8b96ac;font-size:14px;line-height:1.5}}
a{{display:inline-block;margin-top:20px;background:#5b93ff;color:#fff;text-decoration:none;
font-weight:600;padding:11px 20px;border-radius:11px;font-size:14px}}</style></head>
<body><div class="c"><div class="d">{'✓' if tone == 'ok' else '↩'}</div>
<h1>{title}</h1><p>{message}</p>
<a href="/partner">Return to your dashboard</a></div>
<script>try{{setTimeout(function(){{location.href='/partner?billing='+({str(tone=='ok').lower()}?'done':'cancelled')}},2600)}}catch(e){{}}</script>
</body></html>"""
    )


@app.get("/onboarding/success", include_in_schema=False)
async def onboarding_success():
    return _stripe_return_page(
        "Card on file added",
        "Your billing is now active. You're all set to receive live-transfer leads.",
        "ok",
    )


@app.get("/onboarding/cancelled", include_in_schema=False)
async def onboarding_cancelled():
    return _stripe_return_page(
        "Card setup cancelled",
        "No card was added. You can finish this anytime from the Billing tab.",
        "warn",
    )


@app.get("/healthz")
async def health():
    return {
        "status": "ok",
        "env": cfg.env,
        "stripe_configured": bool(cfg.stripe.secret_key),
        "stripe_live": cfg.stripe.is_live if cfg.stripe.secret_key else None,
        "twilio_configured": bool(cfg.twilio.account_sid),
        "retell_configured": bool(cfg.retell.api_key and cfg.retell.agent_id),
        "database": cfg.database_url.split("://")[0],
        "admin_auth_enabled": bool(cfg.admin_auth_token),
    }
