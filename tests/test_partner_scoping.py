"""
The boundary tests: prove that a contractor authenticated through the partner
portal can only ever reach their OWN data, and that the marketplace analytics
endpoint aggregates across the network (the admin surface a contractor must
never see).

These drive the real endpoint functions and the require_contractor dependency
from main.py against a temporary SQLite database -- no HTTP client needed.
"""

import asyncio
import importlib
import sys

import pytest
from fastapi import HTTPException


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scoping.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-123")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
    # Keep providers unconfigured so signup/billing don't reach out to Stripe etc.
    for k in ("STRIPE_SECRET_KEY", "TWILIO_ACCOUNT_SID", "RETELL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("main", None)
    mod = importlib.import_module("main")
    yield mod
    sys.modules.pop("main", None)


def _make_contractor(mod, db, cid, name, bid):
    from db.models import ContractorDB
    c = ContractorDB(
        id=cid, name=name, phone_number="+1713555" + cid[-4:].rjust(4, "0"),
        trade="plumbing", coverage_zips=["77002"], is_active=True, base_bid=bid,
        email=f"{cid}@ex.com", has_valid_billing_mandate=True,
        stripe_payment_method_id="pm_x",
    )
    db.add(c)
    return c


def _make_lead(db, lid, contractor_id, cents, status="billed", urgency="high"):
    from db.models import LeadDB
    db.add(LeadDB(
        id=lid, caller_phone="+19990000000", trade="plumbing", zip_code="77002",
        urgency=urgency, street_address="1 Test St", status=status,
        contractor_id=contractor_id, lead_fee=cents / 100.0,
        billed=(status == "billed"), billed_amount_cents=cents,
    ))


def _seed(mod):
    db = mod.SessionLocal()
    _make_contractor(mod, db, "ct_a", "Apex Plumbing", 65.0)
    _make_contractor(mod, db, "ct_b", "Metro Rooter", 50.0)
    db.commit()
    _make_lead(db, "lead_a1", "ct_a", 6500)
    _make_lead(db, "lead_a2", "ct_a", 9100)
    _make_lead(db, "lead_b1", "ct_b", 5000)
    db.commit()
    return db


def test_partner_leads_return_only_own_rows(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    me_a = db.query(ContractorDB).filter_by(id="ct_a").first()

    leads = asyncio.run(main_mod.partner_leads(me=me_a, db=db))
    ids = {r["id"] for r in leads}

    assert ids == {"lead_a1", "lead_a2"}          # A's leads
    assert "lead_b1" not in ids                    # never B's
    db.close()


def test_partner_billing_totals_only_own_charges(main_mod):
    db = _seed(main_mod)
    from db.models import ContractorDB
    me_a = db.query(ContractorDB).filter_by(id="ct_a").first()
    me_b = db.query(ContractorDB).filter_by(id="ct_b").first()

    bill_a = asyncio.run(main_mod.partner_billing(me=me_a, db=db))
    bill_b = asyncio.run(main_mod.partner_billing(me=me_b, db=db))

    assert bill_a["total_charged_cents"] == 6500 + 9100   # only A
    assert bill_b["total_charged_cents"] == 5000          # only B
    # No card data ever leaves the API -- only a boolean.
    assert set(bill_a.keys()) >= {"billing_active", "has_payment_method"}
    assert "stripe_payment_method_id" not in bill_a
    db.close()


def test_require_contractor_resolves_token_to_right_row(main_mod):
    db = _seed(main_mod)
    token_a = main_mod._partner_tokens.issue("ct_a")
    me = main_mod.require_contractor(authorization=f"Bearer {token_a}", db=db)
    assert me.id == "ct_a"
    db.close()


def test_require_contractor_rejects_bad_tokens(main_mod):
    db = _seed(main_mod)

    # missing header
    with pytest.raises(HTTPException) as e1:
        main_mod.require_contractor(authorization=None, db=db)
    assert e1.value.status_code == 401

    # tampered / unsigned token
    with pytest.raises(HTTPException) as e2:
        main_mod.require_contractor(authorization="Bearer not.a.real.token", db=db)
    assert e2.value.status_code == 401

    # validly-signed token for a contractor that doesn't exist
    ghost = main_mod._partner_tokens.issue("ct_ghost")
    with pytest.raises(HTTPException) as e3:
        main_mod.require_contractor(authorization=f"Bearer {ghost}", db=db)
    assert e3.value.status_code == 401
    db.close()


def test_one_contractors_token_cannot_load_another(main_mod):
    """A token minted for A resolves to A -- never to B."""
    db = _seed(main_mod)
    token_a = main_mod._partner_tokens.issue("ct_a")
    me = main_mod.require_contractor(authorization=f"Bearer {token_a}", db=db)
    assert me.id == "ct_a"
    assert me.id != "ct_b"
    db.close()


def test_admin_analytics_is_network_wide(main_mod):
    """The admin analytics endpoint aggregates across ALL contractors -- this is
    exactly the data partner routes never expose. (Access is gated by
    require_admin, covered in test_auth.py.)"""
    db = _seed(main_mod)
    stats = asyncio.run(main_mod.admin_analytics(db=db, _admin=None))
    assert stats["contractors_total"] == 2
    assert stats["leads_total"] == 3
    assert stats["gross_revenue_cents"] == 6500 + 9100 + 5000
    db.close()
