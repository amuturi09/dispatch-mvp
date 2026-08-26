"""
Unit tests for contractor (partner) auth primitives: password hashing and
signed session tokens. Pure functions, no DB or framework.
"""

import time

import pytest

from partner_auth import (
    hash_password, verify_password, PartnerTokenService, bearer_token,
)


# --- password hashing ------------------------------------------------------

def test_hash_verify_roundtrip():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) is True


def test_verify_rejects_wrong_password():
    h = hash_password("correct horse battery")
    assert verify_password("wrong password here", h) is False


def test_hash_is_salted_unique():
    # Same password hashes differently each time (random salt).
    assert hash_password("abcd1234!") != hash_password("abcd1234!")


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_verify_handles_missing_or_garbage_hash():
    assert verify_password("anything123", None) is False
    assert verify_password("anything123", "not-a-valid-hash") is False


# --- session tokens --------------------------------------------------------

def test_token_roundtrip():
    svc = PartnerTokenService("s3cret")
    tok = svc.issue("ct_abc123")
    assert svc.verify(tok) == "ct_abc123"


def test_token_wrong_secret_rejected():
    good = PartnerTokenService("s3cret").issue("ct_abc123")
    assert PartnerTokenService("different-secret").verify(good) is None


def test_token_tampered_rejected():
    svc = PartnerTokenService("s3cret")
    tok = svc.issue("ct_abc123")
    payload, _, sig = tok.partition(".")
    forged = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    assert svc.verify(forged) is None


def test_token_expired_rejected():
    svc = PartnerTokenService("s3cret")
    tok = svc.issue("ct_abc123", ttl_seconds=-1)  # already expired
    assert svc.verify(tok) is None


def test_token_ids_do_not_cross():
    svc = PartnerTokenService("s3cret")
    a = svc.issue("ct_a")
    b = svc.issue("ct_b")
    assert svc.verify(a) == "ct_a"
    assert svc.verify(b) == "ct_b"
    assert svc.verify(a) != "ct_b"


@pytest.mark.parametrize("bad", [None, "", "garbage", "a.b.c", "noseparator"])
def test_token_malformed_rejected(bad):
    assert PartnerTokenService("s3cret").verify(bad) is None


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        PartnerTokenService("")


# --- bearer header parsing -------------------------------------------------

def test_bearer_token_extracts():
    assert bearer_token("Bearer abc.def") == "abc.def"


@pytest.mark.parametrize("header", [None, "", "abc", "Basic abc", "Bearer a b"])
def test_bearer_token_rejects_bad_headers(header):
    assert bearer_token(header) is None
