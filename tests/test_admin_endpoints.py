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


# --- recover stalled signups: near-misses + billing link ------------------

def test_near_misses_surfaces_covered_but_unonboarded(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB, LeadDB
    c = db.query(ContractorDB).filter_by(id="ct_a").first()   # covers plumbing 77002
    c.approved = False
    c.has_valid_billing_mandate = False
    db.commit()
    db.add(LeadDB(id="lead_nm", caller_phone="+19990000000", trade="plumbing",
                  zip_code="77002", urgency="high", street_address="1 St", status="no_match"))
    db.commit()
    rows = asyncio.run(main_mod.admin_near_misses(db=db, _admin=None))
    hit = [r for r in rows if r["lead_id"] == "lead_nm" and r["contractor_id"] == "ct_a"]
    assert hit, "a covered-but-unonboarded contractor should surface as a near-miss"
    assert "not approved" in hit[0]["blocked_by"] and "no card on file" in hit[0]["blocked_by"]
    db.close()


def test_near_misses_ignores_uncovered_zip(main_mod):
    db = _seed(main_mod)
    from db.models import LeadDB
    db.add(LeadDB(id="lead_nm2", caller_phone="+19990000000", trade="plumbing",
                  zip_code="99999", urgency="high", street_address="1 St", status="no_match"))
    db.commit()
    rows = asyncio.run(main_mod.admin_near_misses(db=db, _admin=None))
    assert all(r["lead_id"] != "lead_nm2" for r in rows)   # nobody covers 99999
    db.close()


def test_near_misses_ignores_prospects(main_mod):
    # A prospect covering the ZIP is NOT a near-miss: it would take the next call
    # as a fallback, so it must never be flagged as "no card / finish onboarding".
    db = _seed(main_mod)
    from db.models import ContractorDB, LeadDB
    db.query(ContractorDB).filter_by(id="ct_a").delete()   # drop the registered one
    db.add(ContractorDB(id="p_only", name="Local Pro", phone_number="+1", trade="plumbing",
                        coverage_zips=["77002"], is_active=True, base_bid=0.0, approved=True,
                        is_prospect=True, free_leads_remaining=2))
    db.add(LeadDB(id="lead_p", caller_phone="+19990000000", trade="plumbing",
                  zip_code="77002", urgency="high", street_address="1 St", status="no_match"))
    db.commit()
    rows = asyncio.run(main_mod.admin_near_misses(db=db, _admin=None))
    assert all(r["contractor_id"] != "p_only" for r in rows)  # prospect never a near-miss
    db.close()


# --- deleting a lead (clear test/no-match rows) ----------------------------

def test_delete_lead_removes_it(main_mod):
    from db.models import LeadDB
    db = _seed(main_mod)
    db.add(LeadDB(id="lead_del", caller_phone="+1", trade="plumbing", zip_code="77002",
                  urgency="high", street_address="1 St", status="no_match"))
    db.commit()
    resp = asyncio.run(main_mod.delete_lead("lead_del", db=db, _admin=None))
    assert resp.status_code == 204
    assert db.query(LeadDB).filter_by(id="lead_del").first() is None
    db.close()


def test_delete_lead_unknown_404(main_mod):
    db = _seed(main_mod)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.delete_lead("nope", db=db, _admin=None))
    assert exc.value.status_code == 404
    db.close()


def test_delete_billed_lead_refused(main_mod):
    # _seed adds lead_1 which is billed -> must not be deletable.
    db = _seed(main_mod)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.delete_lead("lead_1", db=db, _admin=None))
    assert exc.value.status_code == 409
    db.close()


def test_billing_link_requires_stripe_configured(main_mod):
    db = _seed(main_mod)  # Stripe unconfigured in tests -> provider guard
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.contractor_billing_link("ct_a", db=db, _admin=None))
    assert exc.value.status_code == 503
    db.close()


# --- prospect onboarding (free-lead, no Stripe) ----------------------------

def test_onboard_prospect_skips_stripe_and_grants_free_lead(main_mod):
    # Stripe is unconfigured in tests. A regular onboard would 503 on the
    # provider guard; a prospect must bypass Stripe entirely.
    db = main_mod.SessionLocal()
    body = main_mod.ContractorOnboardApi(
        id="p_local", name="Local Plumber", phone_number="+17135550100",
        trade=main_mod.Trade.PLUMBING, coverage_zips=["77002"], base_bid=0.0,
        is_prospect=True,
    )
    out = asyncio.run(main_mod.onboard_contractor(body, db=db, _admin=None))
    assert out["status"] == "prospect_added"
    assert out["free_leads_remaining"] == 2  # default grant
    assert "checkout_url" not in out  # no card-setup link for prospects

    from db.models import ContractorDB
    row = db.query(ContractorDB).filter_by(id="p_local").first()
    assert row.is_prospect is True
    assert row.free_leads_remaining == 2
    assert row.approved is True          # matchable as a fallback
    assert row.has_valid_billing_mandate is False
    assert row.stripe_customer_id is None  # never touched Stripe
    db.close()


def test_onboard_prospect_honors_explicit_free_leads(main_mod):
    db = main_mod.SessionLocal()
    body = main_mod.ContractorOnboardApi(
        id="p_three", name="Three Leads", phone_number="+17135550103",
        trade=main_mod.Trade.PLUMBING, coverage_zips=["77002"], base_bid=0.0,
        is_prospect=True, free_leads=3,
    )
    out = asyncio.run(main_mod.onboard_contractor(body, db=db, _admin=None))
    assert out["free_leads_remaining"] == 3
    db.close()


def test_onboard_non_prospect_still_requires_stripe(main_mod):
    # Guard against the prospect branch accidentally relaxing the paid path.
    db = main_mod.SessionLocal()
    body = main_mod.ContractorOnboardApi(
        id="c_paid", name="Paid Co", phone_number="+17135550200",
        trade=main_mod.Trade.PLUMBING, coverage_zips=["77002"], base_bid=65.0,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.onboard_contractor(body, db=db, _admin=None))
    assert exc.value.status_code == 503
    db.close()


def test_prospect_serialized_in_admin_roster(main_mod):
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    db.add(ContractorDB(
        id="p_1", name="Local Pro", phone_number="+17135550100", trade="plumbing",
        coverage_zips=["77002"], is_active=True, base_bid=0.0, approved=True,
        is_prospect=True, free_leads_remaining=1,
    ))
    db.commit()
    out = main_mod._contractor_admin_dict(db.query(ContractorDB).filter_by(id="p_1").first())
    assert out["is_prospect"] is True
    assert out["free_leads_remaining"] == 1
    db.close()


# --- prospect credential verification tracking -----------------------------

def test_onboard_prospect_records_verification_flags(main_mod):
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    body = main_mod.ContractorOnboardApi(
        id="p_ver", name="Verified Pro", phone_number="+17135550104",
        trade=main_mod.Trade.ELECTRICAL, coverage_zips=["77002"], base_bid=0.0,
        is_prospect=True, license_verified=True, insurance_verified=False,
    )
    asyncio.run(main_mod.onboard_contractor(body, db=db, _admin=None))
    row = db.query(ContractorDB).filter_by(id="p_ver").first()
    assert row.license_verified is True
    assert row.insurance_verified is False
    out = main_mod._contractor_admin_dict(row)
    assert out["license_verified"] is True
    assert out["insurance_verified"] is False
    db.close()


def test_verification_flags_default_false_for_prospect(main_mod):
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    body = main_mod.ContractorOnboardApi(
        id="p_unv", name="Unverified Pro", phone_number="+17135550105",
        trade=main_mod.Trade.PLUMBING, coverage_zips=["77002"], base_bid=0.0,
        is_prospect=True,
    )
    asyncio.run(main_mod.onboard_contractor(body, db=db, _admin=None))
    out = main_mod._contractor_admin_dict(db.query(ContractorDB).filter_by(id="p_unv").first())
    assert out["license_verified"] is False
    assert out["insurance_verified"] is False
    db.close()


def test_admin_can_flip_insurance_verified_later(main_mod):
    # The operator confirms insurance after the prospect sends a certificate.
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    db.add(ContractorDB(
        id="p_flip", name="Later Pro", phone_number="+17135550106", trade="hvac",
        coverage_zips=["77002"], is_active=True, base_bid=0.0, approved=True,
        is_prospect=True, free_leads_remaining=2,
        license_verified=True, insurance_verified=False,
    ))
    db.commit()
    out = asyncio.run(main_mod.update_contractor_admin(
        "p_flip", main_mod.ContractorAdminUpdateApi(insurance_verified=True),
        db=db, _admin=None))
    assert out["insurance_verified"] is True
    assert out["license_verified"] is True  # unchanged
    db.close()


# --- stale-duplicate prospect detection ------------------------------------

def _add_prospect_and_registered(main_mod, prospect_phone, registered_phone):
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    db.add(ContractorDB(
        id="p_dup", name="Apex (prospect)", phone_number=prospect_phone, trade="plumbing",
        coverage_zips=["77002"], is_active=True, base_bid=0.0, approved=True,
        is_prospect=True, free_leads_remaining=2,
    ))
    db.add(ContractorDB(
        id="ct_apex", name="Apex Plumbing LLC", phone_number=registered_phone, trade="plumbing",
        coverage_zips=["77002"], is_active=True, base_bid=55.0, approved=True,
        has_valid_billing_mandate=True,
    ))
    db.commit()
    return db


def test_list_flags_prospect_duplicate_by_phone(main_mod):
    # Prospect and registered share a phone (different formatting) -> flagged.
    db = _add_prospect_and_registered(main_mod, "+1 (346) 555-0199", "3465550199")
    rows = asyncio.run(main_mod.list_contractors(db=db, _admin=None))
    by_id = {r["id"]: r for r in rows}
    assert by_id["p_dup"]["duplicate_of"] == {"id": "ct_apex", "name": "Apex Plumbing LLC"}
    # The registered contractor itself is never flagged as a duplicate.
    assert by_id["ct_apex"]["duplicate_of"] is None
    db.close()


def test_list_no_duplicate_when_phones_differ(main_mod):
    db = _add_prospect_and_registered(main_mod, "+13465550199", "+13465550200")
    rows = asyncio.run(main_mod.list_contractors(db=db, _admin=None))
    by_id = {r["id"]: r for r in rows}
    assert by_id["p_dup"]["duplicate_of"] is None
    db.close()


def test_norm_phone_strips_formatting_and_country_code(main_mod):
    assert main_mod._norm_phone("+1 (346) 555-0199") == "3465550199"
    assert main_mod._norm_phone("346-555-0199") == "3465550199"
    assert main_mod._norm_phone("13465550199") == "3465550199"
    assert main_mod._norm_phone("555-0199") == ""   # too few digits to compare
    assert main_mod._norm_phone(None) == ""


# --- editing a prospect's core profile -------------------------------------

def _seed_prospect(main_mod):
    from db.models import ContractorDB
    db = main_mod.SessionLocal()
    db.add(ContractorDB(
        id="p_edit", name="Old Name", phone_number="+13465550000", trade="plumbing",
        coverage_zips=["77002"], is_active=True, base_bid=0.0, approved=True,
        is_prospect=True, free_leads_remaining=1, reputation_score=4.0,
    ))
    db.commit()
    return db


def test_edit_prospect_core_fields(main_mod):
    db = _seed_prospect(main_mod)
    body = main_mod.ContractorAdminUpdateApi(
        name="New Name", phone_number="+13465559999", trade=main_mod.Trade.HVAC,
        coverage_zips=["77380", " 77381 ", ""], reputation_score=4.7,
        free_leads_remaining=3,
    )
    out = asyncio.run(main_mod.update_contractor_admin("p_edit", body, db=db, _admin=None))
    assert out["name"] == "New Name"
    assert out["phone_number"] == "+13465559999"
    assert out["trade"] == "hvac"
    assert out["zips"] == ["77380", "77381"]   # trimmed, blanks dropped
    assert out["reputation"] == 4.7
    assert out["free_leads_remaining"] == 3
    assert out["is_prospect"] is True          # still a prospect
    db.close()


def test_edit_free_leads_floored_at_zero(main_mod):
    db = _seed_prospect(main_mod)
    out = asyncio.run(main_mod.update_contractor_admin(
        "p_edit", main_mod.ContractorAdminUpdateApi(free_leads_remaining=-5),
        db=db, _admin=None))
    assert out["free_leads_remaining"] == 0
    db.close()


def test_edit_only_changes_sent_fields(main_mod):
    db = _seed_prospect(main_mod)
    out = asyncio.run(main_mod.update_contractor_admin(
        "p_edit", main_mod.ContractorAdminUpdateApi(phone_number="+13465551111"),
        db=db, _admin=None))
    assert out["phone_number"] == "+13465551111"
    assert out["name"] == "Old Name"           # untouched
    assert out["zips"] == ["77002"]            # untouched
    assert out["free_leads_remaining"] == 1    # untouched
    db.close()


# --- renaming the contractor ID (primary key) ------------------------------

def test_rename_contractor_id_no_leads(main_mod):
    from db.models import ContractorDB
    db = _seed_prospect(main_mod)
    out = asyncio.run(main_mod.update_contractor_admin(
        "p_edit", main_mod.ContractorAdminUpdateApi(new_id="bayou_hvac", name="Bayou HVAC"),
        db=db, _admin=None))
    assert out["id"] == "bayou_hvac"
    assert out["name"] == "Bayou HVAC"          # other edits land on the new row
    assert db.query(ContractorDB).filter_by(id="p_edit").first() is None  # old id gone
    assert db.query(ContractorDB).filter_by(id="bayou_hvac").first() is not None
    db.close()


def test_rename_contractor_id_repoints_lead_history(main_mod):
    from db.models import ContractorDB, LeadDB
    db = _seed_prospect(main_mod)
    # A connected free lead for the prospect, plus another lead that lists the
    # prospect in its failover queue.
    db.add(LeadDB(id="ld1", caller_phone="+1999", trade="plumbing", zip_code="77002",
                  urgency="high", street_address="1 St", status="connected_free",
                  contractor_id="p_edit", failover_queue=[]))
    db.add(LeadDB(id="ld2", caller_phone="+1999", trade="plumbing", zip_code="77002",
                  urgency="high", street_address="2 St", status="matched",
                  contractor_id="other", failover_queue=["p_edit", "x"]))
    db.commit()
    asyncio.run(main_mod.update_contractor_admin(
        "p_edit", main_mod.ContractorAdminUpdateApi(new_id="newhandle"), db=db, _admin=None))
    assert db.query(LeadDB).filter_by(id="ld1").first().contractor_id == "newhandle"
    assert db.query(LeadDB).filter_by(id="ld2").first().failover_queue == ["newhandle", "x"]
    db.close()


def test_rename_to_existing_id_conflicts(main_mod):
    from db.models import ContractorDB
    db = _seed_prospect(main_mod)
    db.add(ContractorDB(id="taken", name="Taken", phone_number="+1", trade="hvac",
                        coverage_zips=["77002"], is_active=True, base_bid=0.0,
                        is_prospect=True, approved=True))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.update_contractor_admin(
            "p_edit", main_mod.ContractorAdminUpdateApi(new_id="taken"), db=db, _admin=None))
    assert exc.value.status_code == 409
    db.close()


def test_rename_with_spaces_rejected(main_mod):
    db = _seed_prospect(main_mod)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_mod.update_contractor_admin(
            "p_edit", main_mod.ContractorAdminUpdateApi(new_id="bad id"), db=db, _admin=None))
    assert exc.value.status_code == 400
    db.close()


def test_rename_same_id_is_noop(main_mod):
    db = _seed_prospect(main_mod)
    out = asyncio.run(main_mod.update_contractor_admin(
        "p_edit", main_mod.ContractorAdminUpdateApi(new_id="p_edit", name="Still Here"),
        db=db, _admin=None))
    assert out["id"] == "p_edit"
    assert out["name"] == "Still Here"
    db.close()
