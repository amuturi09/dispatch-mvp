"""
CLI helper to onboard a new contractor: creates the DB row + Stripe customer,
generates their card-setup link, and (optionally) texts it to them via Twilio.

Usage:
    python -m scripts.onboard_contractor \
        --id c_3 --name "Rapid Rooter Co" --phone +17135550300 \
        --trade plumbing --zips 77001,77002 --bid 65 [--sms]

Requires the API server NOT to be running against the same SQLite file
concurrently (SQLite has no real concurrent-writer story) -- for anything
beyond local testing, point this at the same Postgres DATABASE_URL as the
running API instead.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from db.session import make_engine, make_session_factory, init_db
from db.models import ContractorDB
from integrations import stripe_onboarding
from integrations.twilio_telephony import make_twilio_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--phone", required=True, help="E.164 format, e.g. +17135550300")
    parser.add_argument("--trade", required=True, choices=["plumbing", "hvac", "locksmith", "electrical"])
    parser.add_argument("--zips", required=True, help="Comma-separated 5-digit ZIPs")
    parser.add_argument("--bid", required=True, type=float)
    parser.add_argument("--sms", action="store_true", help="Text the setup link via Twilio")
    args = parser.parse_args()

    cfg = load_config(require_all=False)
    if not cfg.stripe.secret_key:
        print("ERROR: STRIPE_SECRET_KEY not set. Set it in .env before onboarding a contractor.")
        sys.exit(1)

    stripe_onboarding.init_stripe(cfg.stripe)
    engine = make_engine(cfg.database_url)
    init_db(engine)
    db = make_session_factory(engine)()

    if db.query(ContractorDB).filter_by(id=args.id).first():
        print(f"ERROR: contractor id '{args.id}' already exists.")
        sys.exit(1)

    stripe_customer_id = stripe_onboarding.create_or_get_stripe_customer(args.id, args.name, args.phone)
    db.add(ContractorDB(
        id=args.id, name=args.name, phone_number=args.phone, trade=args.trade,
        coverage_zips=args.zips.split(","), is_active=True, base_bid=args.bid,
        stripe_customer_id=stripe_customer_id, reputation_score=4.0,
        has_valid_billing_mandate=False,
    ))
    db.commit()

    link = stripe_onboarding.create_onboarding_checkout_session(
        contractor_id=args.id,
        stripe_customer_id=stripe_customer_id,
        success_url=f"{cfg.base_url}/onboarding/success?contractor_id={args.id}",
        cancel_url=f"{cfg.base_url}/onboarding/cancelled?contractor_id={args.id}",
    )

    print(f"Contractor '{args.name}' created ({args.id}).")
    print(f"Card setup link (send this to them): {link.checkout_url}")
    print("They will NOT receive billable leads until they complete this checkout.")

    if args.sms:
        if not cfg.twilio.account_sid:
            print("Skipping SMS: Twilio not configured.")
        else:
            client = make_twilio_client(cfg.twilio)
            client.messages.create(
                to=args.phone,
                from_=cfg.twilio.from_number,
                body=f"Welcome to Dialpatch! Complete card setup to start receiving leads: {link.checkout_url}",
            )
            print("SMS sent.")


if __name__ == "__main__":
    main()
