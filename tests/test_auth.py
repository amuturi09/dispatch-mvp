"""
Unit tests for admin authentication.

Covers Bearer-header parsing, the constant-time token validator, and the
FastAPI dependency wiring produced by make_admin_dependency.
"""

import asyncio

import pytest
from fastapi import HTTPException

from auth import require_admin_auth, AdminAuthValidator, make_admin_dependency


# --- header parsing (require_admin_auth) -----------------------------------

def test_missing_header_rejected():
    with pytest.raises(HTTPException) as exc:
        require_admin_auth(None)
    assert exc.value.status_code == 401


def test_valid_bearer_header_returns_token():
    assert require_admin_auth("Bearer sekret") == "sekret"


@pytest.mark.parametrize("header", [
    "sekret",            # no scheme
    "Basic sekret",      # wrong scheme
    "Bearer a b",        # too many parts
    "Bearer",            # missing token
])
def test_malformed_header_rejected(header):
    with pytest.raises(HTTPException) as exc:
        require_admin_auth(header)
    assert exc.value.status_code == 401


# --- validator -------------------------------------------------------------

def test_validator_accepts_matching_token():
    validator = AdminAuthValidator("expected-token")
    assert validator.validate("expected-token") is True


def test_validator_rejects_wrong_token():
    validator = AdminAuthValidator("expected-token")
    assert validator.validate("nope") is False


def test_validator_raises_when_not_configured():
    validator = AdminAuthValidator(None)
    assert validator.enabled is False
    with pytest.raises(RuntimeError):
        validator.validate("anything")


# --- dependency factory (make_admin_dependency) ----------------------------

def test_dependency_returns_token_when_valid():
    dep = make_admin_dependency(AdminAuthValidator("t0ken"))
    assert asyncio.run(dep(token="t0ken")) == "t0ken"


def test_dependency_rejects_invalid_token():
    dep = make_admin_dependency(AdminAuthValidator("t0ken"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(token="wrong"))
    assert exc.value.status_code == 401
