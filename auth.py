"""
Admin authentication for FastAPI. Protects routes that should only be
accessible to internal/trusted users (e.g. contractor onboarding, listing,
modifying configuration). 

Uses a simple Bearer token approach for MVP -- swap for OAuth2/OIDC/etc. 
before production. Token is set via ADMIN_AUTH_TOKEN env var.

Usage in a route:
    @app.post("/api/v1/contractors/onboard")
    async def onboard_contractor(c: ContractorOnboardApi, db: Session = Depends(get_db), 
                                  admin_token: str = Depends(require_admin_auth)):
        # endpoint body
"""

from fastapi import Depends, HTTPException, Header
from typing import Optional


def require_admin_auth(authorization: Optional[str] = Header(None)) -> str:
    """
    FastAPI dependency that validates the Authorization header.
    Expected format: "Bearer <admin_token>"
    
    Raises HTTPException 401 if missing or invalid.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required.")
    
    parts = authorization.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header format. Use: Bearer <token>")
    
    token = parts[1]
    # Actual validation happens at the app startup level (see below);
    # this function just extracts the token from the header.
    return token


class AdminAuthValidator:
    """Wraps the token validation logic so it can be injected at startup."""
    
    def __init__(self, expected_token: Optional[str]):
        self.expected_token = expected_token
        self.enabled = bool(expected_token)
    
    def validate(self, token: str) -> bool:
        if not self.enabled:
            raise RuntimeError("Admin auth is not configured (ADMIN_AUTH_TOKEN not set).")
        # Constant-time comparison to prevent timing attacks.
        import hmac
        return hmac.compare_digest(token, self.expected_token)


def make_admin_dependency(validator: AdminAuthValidator):
    """
    Returns a FastAPI dependency function that validates the token.
    Bound to a specific validator instance at app startup.
    """
    async def _require_admin(token: str = Depends(require_admin_auth)) -> str:
        if not validator.validate(token):
            raise HTTPException(status_code=401, detail="Invalid token.")
        return token
    return _require_admin
