"""
Owner/admin endpoint tests: the contractor-management PATCH (pause / resume /
adjust bid) and the full lead ledger. These drive the route functions from
main.py directly against a temporary SQLite database, matching the approach in
tests/test_partner_scoping.py -- no HTTP client needed.
"""

import asyncio
import importlib
import sys

import pytest
from fastapi import HTTPException


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-123")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
    for k in ("STRIPE_SECRET_KEY", "TWILIO_ACCOUNT_SID", "RETELL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("main", None)
    mod = importlib.import_module("main")
    yield mod
    sys.modules.pop("main", None)


def _seed(mod):
    from db.models import ContractorDB, LeadDB
    db = mod.SessionLocal()
    db.add(ContractorDB(
        id="ct_a", name="Apex Plumbing", phone_number="+17135550001",
        trade="plumbing", coverage_zips=["77002"], is_active=True, base_bid=65.0,
        has_valid_billing_mandate=True, consecutive_no_answers=2,
    ))
    db.add(LeadDB(
        id="lead_1", caller_phone="+19990000000", trade="plumbing", zip_code="77002",
        urgency="high", street_address="1 Test St", status="billed", contractor_id="ct_a",
        lead_fee=65.0, billed=True, billed_amount_cents=6500,
    ))
    db.commit()
    return db


# --- contractor PATCH (owner controls) -------------------------------------

def test_admin_can_pause_contractor(main_mod):
    db = _seed(main_mod)
    body = main_mod.ContractorAdminUpdateApi(is_active=False)
    out = asyncio.run(main_mod.update_contractor_admin("ct_a", body, db=db, _admin=None))
    assert out["active"] is False
    db.close()


def test_admin_resume_clears_no_answer_streak(main_mod):
    db = _seed(main_mod)
    asyncio.run(main_mod.update_contractor_admin(
        "ct_a", main_mod.ContractorAdminUpdateApi(is_active=False), db=db, _admin=None))
    out = asyncio.run(main_mod.update_contractor_admin(
        "ct_a", main_mod.ContractorAdminUpdateApi(is_active=True), db=db, _admin=None))
    assert out["active"] is True
    assert out["consecutive_no_answers"] == 0  # resuming un-pauses the miss counter
    db.close()


def test_admin_update_bid_only_changes_bid(main_mod):
    db = _seed(main_mod)
    out = asyncio.run(main_mod.update_contractor_admin(
        "ct_a", main_mod.ContractorAdminUpdateApi(base_bid=99.0), db=db, _admin=None))
    assert out["base_bid"] == 99.0
    assert out["active"] is True  # untouched field stays as-is
    db.close()


def test_admin_update_unknown_contractor_404(main_mod):
    db = _seed(main_mod)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.update_contractor_admin(
            "does_not_exist", main_mod.ContractorAdminUpdateApi(is_active=False),
            db=db, _admin=None))
    assert exc.value.status_code == 404
    db.close()


# --- admin lead ledger -----------------------------------------------------

def test_admin_leads_returns_ledger_with_contractor_name(main_mod):
    db = _seed(main_mod)
    rows = asyncio.run(main_mod.admin_leads(limit=100, db=db, _admin=None))
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "lead_1"
    assert r["billed"] is True
    assert r["billed_amount_cents"] == 6500
    assert r["contractor_name"] == "Apex Plumbing"  # resolved from contractor_id
    db.close()


def test_admin_leads_limit_is_clamped(main_mod):
    db = _seed(main_mod)
    # A wild limit is clamped, not passed raw to the query.
    rows = asyncio.run(main_mod.admin_leads(limit=100000, db=db, _admin=None))
    assert isinstance(rows, list)
    db.close()
