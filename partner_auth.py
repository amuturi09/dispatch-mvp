"""
Contractor (partner) authentication.

Separate from auth.py, which guards the operator/admin surface. This module is
the contractor side: password hashing for self-service login and short-lived,
HMAC-signed session tokens. Both use only the standard library -- no bcrypt or
JWT dependency to pull in for the MVP.

Design notes:
- Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt. Never store
  or log the raw password.
- Session tokens are stateless and signed with SESSION_SECRET (see config.py).
  They carry only the contractor id and an expiry; the signature makes them
  unforgeable. Because they're stateless there's no server-side revocation --
  keep the TTL modest and rotate SESSION_SECRET to invalidate everything.
- The FastAPI dependency that turns a token into a scoped ContractorDB row lives
  in main.py, where the DB session factory is available. This module stays
  free of framework and DB imports so it's trivially unit-testable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional

# --- password hashing -------------------------------------------------------

_PBKDF2_ROUNDS = 200_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """Return a self-describing hash string safe to persist."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{_ALGO}${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time verification against a hash produced by hash_password."""
    if not stored:
        return False
    try:
        algo, rounds_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(dk, expected)


# --- session tokens ---------------------------------------------------------

_DEFAULT_TTL = 60 * 60 * 12  # 12 hours


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class PartnerTokenService:
    """Issues and verifies stateless, signed contractor session tokens."""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("PartnerTokenService requires a non-empty secret.")
        self._secret = secret.encode("utf-8")

    def _sign(self, payload_b64: str) -> str:
        sig = hmac.new(self._secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return _b64u(sig)

    def issue(self, contractor_id: str, ttl_seconds: int = _DEFAULT_TTL) -> str:
        expiry = int(time.time()) + int(ttl_seconds)
        payload = f"{contractor_id}:{expiry}".encode("utf-8")
        payload_b64 = _b64u(payload)
        return f"{payload_b64}.{self._sign(payload_b64)}"

    def verify(self, token: Optional[str]) -> Optional[str]:
        """Return the contractor id if the token is valid and unexpired, else None."""
        if not token or "." not in token:
            return None
        payload_b64, _, sig = token.partition(".")
        expected_sig = self._sign(payload_b64)
        # Constant-time signature check before trusting any payload bytes.
        if not hmac.compare_digest(sig, expected_sig):
            return None
        try:
            contractor_id, expiry_s = _b64u_decode(payload_b64).decode("utf-8").rsplit(":", 1)
            expiry = int(expiry_s)
        except (ValueError, UnicodeDecodeError):
            return None
        if time.time() >= expiry:
            return None
        return contractor_id


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an 'Authorization: Bearer <token>' header."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        return None
    return parts[1]
