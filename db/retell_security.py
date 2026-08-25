"""
Retell AI integration: verifies that inbound requests (function/tool calls
during a live conversation, and post-call webhooks) actually came from Retell.

Important quirk: Retell signs with your RETELL_API_KEY itself (the one with
the "webhook" badge in the dashboard), not a separate webhook secret. That
means this key is both an outbound credential (if you call Retell's API) and
an inbound verification secret -- treat it accordingly (rotate carefully,
never log it).

Signature format: header `X-Retell-Signature: v={unix_ms_timestamp},d={hex_hmac_sha256}`
computed over `raw_body + timestamp`, using RETELL_API_KEY as the HMAC key.
"""

from __future__ import annotations
import hmac
import hashlib
import time


class RetellVerificationError(Exception):
    pass


def verify_retell_signature(raw_body: bytes, signature_header: str, api_key: str, max_skew_seconds: int = 300) -> bool:
    """
    Verifies X-Retell-Signature without depending on the Retell SDK being
    installed (implemented directly against their documented scheme, so this
    keeps working even if you're not ready to add the SDK as a dependency).

    Prefer the official `retell` Python SDK's `client.verify(...)` if you
    already have it installed -- this is a compatible fallback.
    """
    if not signature_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        timestamp = parts["v"]
        digest_hex = parts["d"]
    except (ValueError, KeyError):
        raise RetellVerificationError(f"Malformed X-Retell-Signature header: {signature_header!r}")

    # Replay protection -- reject signatures older than max_skew_seconds.
    now_ms = int(time.time() * 1000)
    try:
        ts_ms = int(timestamp)
    except ValueError:
        raise RetellVerificationError("Non-numeric timestamp in signature header.")
    if abs(now_ms - ts_ms) > max_skew_seconds * 1000:
        raise RetellVerificationError("Signature timestamp outside allowed window (possible replay).")

    signed_payload = raw_body + timestamp.encode("utf-8")
    expected_digest = hmac.new(api_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected_digest, digest_hex)
