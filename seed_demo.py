"""
Seed a demo contractor + sample leads so the partner portal has something to
show on first run. Safe to re-run: it wipes and recreates the demo account.

    python seed_demo.py

Then open http://localhost:8000/partner and sign in with:
    email:    demo@dialpatch.test
    password: demo12345
"""

from __future__ import annotations
import datetime as dt

from config import load_config
from db.session import make_engine, make_session_factory, init_db
from db.models import ContractorDB, LeadDB
from partner_auth import hash_password

DEMO_EMAIL = "demo@dialpatch.test"
DEMO_PASSWORD = "demo12345"
DEMO_ID = "ct_demo0001"


def _utc(days_ago: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)


# (id_suffix, days_ago, zip, urgency, status, duration_s, fee, billed_cents)
LEADS = [
    ("a1", 0.05, "77002", "critical", "bridged", 0,   65.0, None),   # in progress
    ("a2", 0.20, "77005", "high",     "billed",  254, 65.0, 6500),
    ("a3", 0.35, "77002", "standard", "billed",  188, 65.0, 6500),
    ("a4", 1.10, "77002", "critical", "billed",  281, 91.0, 9100),   # surge
    ("a5", 1.40, "77005", "high",     "billed",  101, 65.0, 6500),
    ("a6", 2.05, "77019", "standard", "bridged", 74,  95.0, None),
    ("a7", 2.60, "77001", "high",     "matched", 0,   65.0, None),   # matched, not connected
    ("a8", 3.20, "77002", "critical", "billed",  312, 65.0, 6500),
]


def main() -> None:
    cfg = load_config(require_all=False)
    engine = make_engine(cfg.database_url)
    init_db(engine)
    Session = make_session_factory(engine)
    db = Session()

    # Wipe any prior demo data for a clean, repeatable seed.
    db.query(LeadDB).filter(LeadDB.contractor_id == DEMO_ID).delete()
    db.query(ContractorDB).filter(ContractorDB.id == DEMO_ID).delete()
    db.commit()

    db.add(ContractorDB(
        id=DEMO_ID,
        name="Apex 24/7 Plumbing",
        owner_name="Jordan Rivera",
        phone_number="+17135550199",
        trade="plumbing",
        coverage_zips=["77002", "77005", "77019", "77001"],
        is_active=True,
        base_bid=65.0,
        email=DEMO_EMAIL,
        password_hash=hash_password(DEMO_PASSWORD),
        sms_opt_in=True,
        stripe_customer_id="cus_demo",
        stripe_payment_method_id="pm_demo",   # marks a card as on file
        has_valid_billing_mandate=True,
        reputation_score=4.9,
    ))

    for suffix, days, zc, urg, status, dur, fee, billed_cents in LEADS:
        db.add(LeadDB(
            id=f"lead_{suffix}",
            caller_phone="+17135551000",
            trade="plumbing",
            zip_code=zc,
            urgency=urg,
            street_address="1420 Rusk St",
            status=status,
            contractor_id=DEMO_ID,
            lead_fee=fee,
            call_duration_seconds=dur,
            call_status="completed" if billed_cents else None,
            billed=bool(billed_cents),
            billed_amount_cents=billed_cents,
            created_at=_utc(days),
        ))

    db.commit()
    db.close()

    print("Seeded demo contractor + %d leads." % len(LEADS))
    print(f"  DB:       {cfg.database_url}")
    print(f"  Sign in:  {DEMO_EMAIL}  /  {DEMO_PASSWORD}")
    print("  Portal:   http://localhost:8000/partner")


if __name__ == "__main__":
    main()
