"""
Retell custom-telephony integration.

Retell's recommended path for a provider they don't have elastic SIP trunking
set up with (or where you want to keep call control in Twilio, as this
architecture does for the warm-transfer state machine) is "Dial to SIP URI":
  1. Your webhook calls Retell's Register Phone Call API.
  2. Retell returns a call_id.
  3. You dial sip:{call_id}@{retell_sip_domain} within 5 minutes.
  4. Retell's agent handles the conversation over that SIP leg.

Important limitation (from Retell's own docs): with this method Retell does
NOT have access to your telephony provider, so its built-in "transfer call"
feature is unavailable. That's fine here -- the agent simply ends the call
after triage, which causes Twilio's <Dial action=...> callback to fire, and
*our* TwiML (see api/main.py's /webhooks/twilio/post-triage) takes over for
the actual warm transfer. This is why the voice prompt says "end the call"
rather than "transfer the call" after a successful match.

Reference: https://docs.retellai.com/deploy/custom-telephony
"""

from __future__ import annotations
import requests
from dataclasses import dataclass

RETELL_API_BASE = "https://api.retellai.com"


class RetellApiError(RuntimeError):
    pass


@dataclass
class RegisteredCall:
    call_id: str
    sip_uri: str


def register_phone_call(
    api_key: str,
    agent_id: str,
    from_number: str,
    to_number: str,
    sip_domain: str,
    direction: str = "inbound",
    metadata: dict | None = None,
) -> RegisteredCall:
    """
    Tells Retell which agent should handle this call and gets back a call_id
    to build the SIP URI you'll dial the caller's leg into.

    `sip_domain` is account-specific (shown in your Retell dashboard under
    the custom-telephony setup for this agent) -- set it via RETELL_SIP_DOMAIN
    in .env rather than hardcoding, since it varies per Retell account.
    """
    resp = requests.post(
        f"{RETELL_API_BASE}/v2/register-phone-call",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "agent_id": agent_id,
            "from_number": from_number,
            "to_number": to_number,
            "direction": direction,
            "metadata": metadata or {},
        },
        timeout=10,
    )
    if resp.status_code != 201 and resp.status_code != 200:
        raise RetellApiError(f"Register Phone Call failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    call_id = data.get("call_id")
    if not call_id:
        raise RetellApiError(f"Register Phone Call response missing call_id: {data}")

    return RegisteredCall(call_id=call_id, sip_uri=f"sip:{call_id}@{sip_domain}")
