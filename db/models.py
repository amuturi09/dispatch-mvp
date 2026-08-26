"""
SQLAlchemy models. Replaces the in-memory dict store from the first MVP pass --
required before any real billing happens, since a server restart shouldn't be
able to silently erase a contractor's billing mandate status or a lead's audit trail.
"""

from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Float, Boolean, Integer, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class ContractorDB(Base):
    __tablename__ = "contractors"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    phone_number = Column(String, nullable=False)
    trade = Column(String, nullable=False, index=True)
    coverage_zips = Column(JSON, nullable=False, default=list)  # list[str]
    is_active = Column(Boolean, default=True)  # doubles as the "on-call" switch
    base_bid = Column(Float, nullable=False)

    # Contractor self-service login. Nullable because contractors created by an
    # admin (via /api/v1/contractors/onboard) may not have set a password yet;
    # only self-signup partners (via /api/v1/partner/signup) populate these.
    # Email uniqueness is enforced at the application layer (see main.py) so that
    # multiple NULL-email admin rows remain valid.
    email = Column(String, nullable=True, index=True)
    owner_name = Column(String, nullable=True)  # contact person; distinct from the business name
    password_hash = Column(String, nullable=True)
    sms_opt_in = Column(Boolean, default=True)

    # Stripe identifiers -- stripe_customer_id is created immediately on
    # sign-up; has_valid_billing_mandate flips True only after the contractor
    # actually completes a Stripe Checkout Session in setup mode.
    stripe_customer_id = Column(String, nullable=True)
    stripe_payment_method_id = Column(String, nullable=True)
    has_valid_billing_mandate = Column(Boolean, default=False)

    reputation_score = Column(Float, default=4.0)
    consecutive_no_answers = Column(Integer, default=0)
    max_consecutive_no_answers = Column(Integer, default=3)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    leads = relationship("LeadDB", back_populates="contractor")


class LeadDB(Base):
    """Every triage attempt, matched or not -- the audit trail for disputes/debugging."""
    __tablename__ = "leads"

    id = Column(String, primary_key=True)  # uuid, matches core.engine's lead_id
    caller_phone = Column(String, nullable=False)
    trade = Column(String, nullable=False)
    zip_code = Column(String, nullable=False)
    urgency = Column(String, nullable=False)
    street_address = Column(String, nullable=False)

    status = Column(String, nullable=False, index=True)
    disclosure_acknowledged = Column(Boolean, default=False)
    safety_flag_text = Column(String, nullable=True)

    contractor_id = Column(String, ForeignKey("contractors.id"), nullable=True)
    lead_fee = Column(Float, nullable=True)
    whisper_text = Column(String, nullable=True)  # the exact message played to contractor

    # Failover queue: JSON array of contractor IDs, in order tried (oldest first)
    # When a contractor declines/no-answer, we pop from this list and dial the next
    failover_queue = Column(JSON, nullable=False, default=list)  # list[str] of contractor IDs

    call_duration_seconds = Column(Integer, nullable=True)
    call_status = Column(String, nullable=True)
    billed = Column(Boolean, default=False)
    billed_amount_cents = Column(Integer, nullable=True)
    stripe_payment_intent_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    contractor = relationship("ContractorDB", back_populates="leads")


class CallSessionDB(Base):
    """
    Links the three identifiers that exist for a single phone call across
    systems: Twilio's CallSid (the PSTN leg we control), Retell's call_id
    (the SIP leg Retell controls during triage), and our own lead_id (created
    once triage completes). Without this row, there's no way to know which
    lead resulted from a given inbound Twilio call once Retell's leg ends.
    """
    __tablename__ = "call_sessions"

    twilio_call_sid = Column(String, primary_key=True)
    retell_call_id = Column(String, nullable=True, index=True)
    lead_id = Column(String, nullable=True, index=True)
    caller_phone = Column(String, nullable=True)
    contractor_call_sid = Column(String, nullable=True)  # set once we dial a contractor for this lead
    created_at = Column(DateTime, default=utcnow)


class WebhookEventDB(Base):
    """
    Idempotency ledger. Twilio, Stripe, and Retell can all retry webhook
    delivery -- without this, a retried call-status-webhook could double-bill
    a contractor.
    """
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True)  # provider's event id (Stripe event id, Twilio CallSid+status, etc.)
    provider = Column(String, nullable=False)
    received_at = Column(DateTime, default=utcnow)
