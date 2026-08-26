"""
Stripe integration.

Two responsibilities:
1. Onboard a contractor's card for later off-session billing, using a Stripe
   Checkout Session in `mode=setup` -- this is Stripe's current recommended
   pattern for "save a card now, charge it later without the customer present"
   (see https://docs.stripe.com/payments/save-and-reuse). It requires the
   contractor to complete a hosted Stripe page once; after that we hold a
   reusable PaymentMethod ID.
2. Verify and handle Stripe webhooks with the signing secret, so nobody can
   forge a "payment succeeded" event at your API.

Nothing in this file runs against real Stripe unless STRIPE_SECRET_KEY is set
in your environment -- see .env.example / CREDENTIALS_SETUP.md.
"""

from __future__ import annotations
import stripe
from dataclasses import dataclass
from config import StripeConfig


@dataclass
class ContractorOnboardingLink:
    contractor_id: str
    checkout_url: str
    checkout_session_id: str


def init_stripe(cfg: StripeConfig) -> None:
    stripe.api_key = cfg.secret_key


def create_or_get_stripe_customer(contractor_id: str, name: str, phone: str) -> str:
    """Idempotent-ish customer creation, keyed by contractor_id in metadata."""
    existing = stripe.Customer.search(query=f"metadata['contractor_id']:'{contractor_id}'")
    if existing.data:
        return existing.data[0].id
    customer = stripe.Customer.create(
        name=name,
        phone=phone,
        metadata={"contractor_id": contractor_id},
    )
    return customer.id


def create_onboarding_checkout_session(
    contractor_id: str,
    stripe_customer_id: str,
    success_url: str,
    cancel_url: str,
) -> ContractorOnboardingLink:
    """
    Creates a hosted Stripe Checkout page in setup mode. Send this URL to the
    contractor (e.g. via SMS at sign-up) -- they enter card details once,
    Stripe handles PCI compliance and any required authentication, and we
    never touch raw card data.
    """
    session = stripe.checkout.Session.create(
        mode="setup",
        customer=stripe_customer_id,
        payment_method_types=["card"],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"contractor_id": contractor_id},
    )
    return ContractorOnboardingLink(
        contractor_id=contractor_id,
        checkout_url=session.url,
        checkout_session_id=session.id,
    )


def resolve_completed_setup(checkout_session_id: str) -> dict:
    """
    Call this from the checkout.session.completed webhook handler. Retrieves
    the SetupIntent created during checkout and returns the reusable
    PaymentMethod ID that's now safe to charge off-session.
    """
    session = stripe.checkout.Session.retrieve(checkout_session_id, expand=["setup_intent"])
    setup_intent = session.setup_intent
    # session.metadata is a StripeObject, not a plain dict -- the current SDK
    # rejects dict methods like .get() on it, so convert first.
    metadata = session.metadata.to_dict() if session.metadata else {}
    return {
        "contractor_id": metadata.get("contractor_id"),
        "customer_id": session.customer,
        "payment_method_id": setup_intent.payment_method,
        "setup_intent_status": setup_intent.status,
    }


def charge_off_session(customer_id: str, payment_method_id: str, amount_cents: int, lead_id: str) -> dict:
    """
    The actual lead-fee charge. Requires a customer + payment_method that
    completed a setup Checkout Session (mandate captured there satisfies the
    "customer permission for future off-session charges" requirement Stripe
    documents).
    """
    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        customer=customer_id,
        payment_method=payment_method_id,
        off_session=True,
        confirm=True,
        metadata={"lead_id": lead_id},
    )
    return {"id": intent.id, "status": intent.status}


def verify_and_parse_webhook(payload: bytes, sig_header: str, webhook_signing_secret: str) -> "stripe.Event":
    """
    Raises stripe.error.SignatureVerificationError if the payload wasn't
    actually sent by Stripe. Never process a Stripe webhook body without
    calling this first.
    """
    return stripe.Webhook.construct_event(payload, sig_header, webhook_signing_secret)
