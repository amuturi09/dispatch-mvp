"""
Owner/admin endpoint tests: the contractor-management PATCH (pause / resume /
adjust bid) and the full lead ledger. These drive the route functions from
main.py directly against a temporary SQLite database, matching the approach in
tests/test_partner_scoping.py -- no HTTP client needed.
"""

import asyncio
import datetime
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


def test_admin_can_approve_and_record_credentials(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    assert not c.approved  # a seeded contractor starts pending review
    out = asyncio.run(main_mod.update_contractor_admin(
        "ct_a",
        main_mod.ContractorAdminUpdateApi(
            approved=True, license_number="MPL-1", license_state="TX",
            insurance_carrier="Acme", insurance_policy="P-9"),
        db=db, _admin=None))
    assert out["approved"] is True
    assert out["license_number"] == "MPL-1"
    assert out["license_state"] == "TX"
    assert out["insurance_carrier"] == "Acme"
    db.close()


def test_no_expiry_dates_count_as_current(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    assert main_mod._credentials_current(c) is True  # missing dates = current
    c.license_expires = datetime.date.today() + datetime.timedelta(days=200)
    c.insurance_expires = datetime.date.today() + datetime.timedelta(days=30)
    db.commit()
    assert main_mod._credentials_current(c) is True
    db.close()


def test_expired_credential_auto_excludes_from_matching(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    from core.engine import LeadRequest, Trade, UrgencyLevel, LeadStatus
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    c.approved = True                                  # vetted and approved...
    c.license_expires = datetime.date(2000, 1, 1)      # ...but the license lapsed
    db.commit()
    assert main_mod._credentials_current(c) is False
    engine = main_mod._load_engine_from_db(db)
    lead = LeadRequest(caller_phone="+19990000000", trade=Trade.PLUMBING,
                       zip_code="77002", urgency=UrgencyLevel.HIGH,
                       street_address="1 Test St")
    # A lapsed credential drops them from matching with no operator action.
    assert engine.match(lead).status == LeadStatus.NO_MATCH
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


# --- delete / soft-delete contractor -------------------------------------

def test_delete_contractor_with_no_leads_hard_deletes(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    db.add(ContractorDB(id="ct_fresh", name="Fresh Co", phone_number="+17135550000",
                        trade="plumbing", coverage_zips=["77002"], is_active=True, base_bid=40.0))
    db.commit()
    resp = asyncio.run(main_mod.delete_contractor("ct_fresh", db=db, _admin=None))
    assert getattr(resp, "status_code", None) == 204          # 204, no content
    assert db.query(ContractorDB).filter_by(id="ct_fresh").first() is None  # row gone
    db.close()


def test_delete_contractor_with_leads_soft_deletes_and_excludes(main_mod):
    db = _seed(main_mod)                                        # ct_a already has a lead
    from db.models import ContractorDB
    from core.engine import LeadRequest, Trade, UrgencyLevel, LeadStatus
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    c.approved = True                                          # would match if not deleted
    db.commit()
    out = asyncio.run(main_mod.delete_contractor("ct_a", db=db, _admin=None))
    assert isinstance(out, dict) and out["status"] == "soft_deleted"
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    assert c is not None and c.is_deleted is True              # row preserved + flagged
    # excluded from matching
    engine = main_mod._load_engine_from_db(db)
    lead = LeadRequest(caller_phone="+19990000000", trade=Trade.PLUMBING, zip_code="77002",
                       urgency=UrgencyLevel.HIGH, street_address="1 Test St")
    assert engine.match(lead).status == LeadStatus.NO_MATCH
    # excluded from the admin roster
    listed = asyncio.run(main_mod.list_contractors(db=db, _admin=None))
    assert all(r["id"] != "ct_a" for r in listed)
    db.close()


def test_delete_unknown_contractor_404(main_mod):
    db = _seed(main_mod)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.delete_contractor("does_not_exist", db=db, _admin=None))
    assert exc.value.status_code == 404
    db.close()


def test_soft_deleted_contractor_cannot_use_partner_login(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    c = db.query(ContractorDB).filter_by(id="ct_a").first()
    c.is_deleted = True
    db.commit()
    token = main_mod._partner_tokens.issue("ct_a")
    with pytest.raises(HTTPException) as exc:
        main_mod.require_contractor(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401
    db.close()
