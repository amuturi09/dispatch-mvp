"""
Twilio integration: verifying inbound webhook authenticity, and generating
the TwiML / REST calls that implement the warm-transfer state machine
(comfort hold -> outbound whisper dial -> bridge on keypress -> failover).
"""

from __future__ import annotations
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Dial, Gather
from config import TwilioConfig


def make_twilio_client(cfg: TwilioConfig) -> Client:
    return Client(cfg.account_sid, cfg.auth_token)


def verify_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """
    Call this on EVERY inbound Twilio webhook (call-status callbacks, etc.)
    before trusting the payload. `url` must be the exact public URL Twilio
    was configured to POST to, including query string -- mismatches here are
    the most common cause of false verification failures.
    """
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)


# ---------------------------------------------------------------------------
# TwiML generators -- these are what your /webhooks/twilio/* routes return,
# telling Twilio what to do with the live call.
#
# Design: both legs join the same named Conference. The caller leg joins
# first with start_conference_on_enter=False, so they hear hold music but
# audio doesn't bridge yet. The contractor leg joins with
# start_conference_on_enter=True after pressing 1, which is the moment the
# conference actually "starts" (both parties become audible to each other)
# and the billable duration clock effectively begins. Either party leaving
# ends the conference for both (end_conference_on_exit=True), which avoids
# needing a separate REST call to redirect the caller mid-hold.
# ---------------------------------------------------------------------------

def conference_name_for_lead(lead_id: str) -> str:
    return f"lead-{lead_id}"


def twiml_caller_hold(lead_id: str, hold_music_url: str = "") -> str:
    """
    Leg A (caller): returned as the action-callback response once Retell's
    triage leg ends. Puts the caller into the conference in a "waiting" state
    -- they hear hold music but aren't bridged to anyone yet.
    """
    vr = VoiceResponse()
    vr.say("Thanks — we're connecting you with a local contractor now. Please stay on the line.")
    dial = Dial()
    conf_kwargs = dict(start_conference_on_enter=False, end_conference_on_exit=True)
    if hold_music_url:
        conf_kwargs["wait_url"] = hold_music_url
    dial.conference(conference_name_for_lead(lead_id), **conf_kwargs)
    vr.append(dial)
    return str(vr)


def twiml_whisper_and_bridge(lead_id: str, whisper_message: str, gather_action_url: str) -> str:
    """
    Played to Leg B (the contractor) the moment they answer. They must press
    1 to bridge -- this is what prevents an accidental pocket-answer from
    connecting a live caller, and it's the moment that starts the billable
    duration clock once bridged.
    """
    vr = VoiceResponse()
    gather = Gather(num_digits=1, action=gather_action_url, method="POST", timeout=8)
    gather.say(whisper_message)
    vr.append(gather)
    # If no digit pressed within timeout, Twilio falls through here -- treat as a decline.
    vr.say("No response received. Goodbye.")
    vr.hangup()
    return str(vr)


def twiml_bridge_confirmed(lead_id: str) -> str:
    """Returned from the Gather action URL once the contractor presses 1 -- joins them into the conference."""
    vr = VoiceResponse()
    dial = Dial()
    dial.conference(conference_name_for_lead(lead_id), start_conference_on_enter=True, end_conference_on_exit=True)
    vr.append(dial)
    return str(vr)


def twiml_decline_or_timeout() -> str:
    """Returned to the CONTRACTOR leg when they press anything other than 1, or the whisper Gather times out."""
    vr = VoiceResponse()
    vr.say("Lead declined.")
    vr.hangup()
    return str(vr)


def twiml_apology_and_hangup(message: str) -> str:
    """
    Used to end the CALLER leg gracefully -- e.g. no contractor available,
    or all failover candidates exhausted. Returned either directly (if the
    caller leg hasn't joined the conference yet) or via a REST call to
    redirect an in-progress call (see end_call_with_message below).
    """
    vr = VoiceResponse()
    vr.say(message)
    vr.hangup()
    return str(vr)


def twiml_safety_escalation() -> str:
    """Returned to the CALLER leg when the safety guard fires -- never proceeds to matching."""
    vr = VoiceResponse()
    vr.say("Please hang up and dial 911 right now if this is a life-threatening emergency. Goodbye.")
    vr.hangup()
    return str(vr)


# ---------------------------------------------------------------------------
# Outbound call origination (the actual "dial Leg B" step)
# ---------------------------------------------------------------------------

def dial_contractor(
    client: Client,
    from_number: str,
    contractor_phone: str,
    whisper_twiml_url: str,
    status_callback_url: str,
) -> str:
    """
    Places the outbound call to Leg B (contractor). Returns the Twilio
    CallSid, which you should store against the lead so the status callback
    and failover logic can look it up.
    """
    call = client.calls.create(
        to=contractor_phone,
        from_=from_number,
        url=whisper_twiml_url,             # Twilio fetches TwiML from here once answered
        status_callback=status_callback_url,
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
        timeout=15,                         # ~4 rings, matches the blueprint's failover trigger
    )
    return call.sid


def move_caller_into_conference(client: Client, caller_call_sid: str, conference_name: str) -> None:
    """
    Redirects Leg A (the caller, currently on comfort hold) into the same
    conference room the contractor was bridged into. Called once the
    contractor's Gather confirms with keypress '1'.
    """
    dial = Dial()
    dial.conference(conference_name, start_conference_on_enter=True, end_conference_on_exit=True)
    vr = VoiceResponse()
    vr.append(dial)
    client.calls(caller_call_sid).update(twiml=str(vr))


# ---------------------------------------------------------------------------
# SMS notifications
# ---------------------------------------------------------------------------

def send_lead_notification_sms(client: Client, from_number: str, contractor_phone: str, lead_details: dict) -> str:
    """
    Sends SMS to contractor when a lead is matched.
    Returns the SMS message SID.
    """
    message = (
        f"RapidDispatch: {lead_details['trade'].upper()} emergency in {lead_details['zip_code']}. "
        f"Urgency: {lead_details['urgency'].upper()}. "
        f"Lead fee: ${lead_details['lead_fee']:.2f}. "
        f"Answer the next call to accept."
    )
    msg = client.messages.create(
        from_=from_number,
        to=contractor_phone,
        body=message,
    )
    return msg.sid
