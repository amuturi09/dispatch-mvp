"""
Core dispatch engine: contractor matching, surge pricing, and settlement logic.

Deliberately framework-agnostic (no FastAPI/SQLAlchemy imports) so it can be:
  - unit tested in isolation
  - reused by the FastAPI layer (see api/main.py)
  - reused by a future queue worker / different framework without rewriting logic

This is the part of the system that actually decides "who gets paid what,
and under what conditions" -- so it's the part most worth getting right and
testing, rather than leaving inline in route handlers.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class Trade(str, Enum):
    PLUMBING = "plumbing"
    HVAC = "hvac"
    LOCKSMITH = "locksmith"
    ELECTRICAL = "electrical"


class UrgencyLevel(str, Enum):
    CRITICAL = "critical"   # active flooding, no heat in freezing temps, locked out with kids in car, etc.
    HIGH = "high"
    STANDARD = "standard"


class LeadStatus(str, Enum):
    CREATED = "created"
    MATCHED = "matched"
    NO_MATCH = "no_match"
    TRANSFERRING = "transferring"
    BRIDGED = "bridged"
    FAILED_ALL_CONTRACTORS = "failed_all_contractors"
    BILLED = "billed"
    BILLING_FAILED = "billing_failed"
    FLAGGED_SAFETY = "flagged_safety"  # gas/fire/medical -> routed to 911 messaging, never billed


@dataclass
class Contractor:
    id: str
    name: str
    phone_number: str
    trade: Trade
    coverage_zips: set[str]
    is_active: bool
    base_bid: float
    stripe_customer_id: str
    reputation_score: float  # 0.0 - 5.0
    # Consent/compliance: contractor must have an active billing mandate
    # on file (Stripe SetupIntent completed) before they can be auto-charged.
    has_valid_billing_mandate: bool = True
    consecutive_no_answers: int = 0
    max_consecutive_no_answers: int = 3  # auto-pause after this many misses


@dataclass
class LeadRequest:
    caller_phone: str
    trade: Trade
    zip_code: str
    urgency: UrgencyLevel
    street_address: str
    # Explicit caller acknowledgement, collected by the voice agent, that
    # this is a paid referral service and a contractor will be sent.
    # Blueprint versions never asked the *homeowner* for anything --
    # only billed the contractor silently. We still don't charge the
    # homeowner, but we do require they were told what's happening.
    disclosure_acknowledged: bool = False


@dataclass
class MatchResult:
    lead_id: str
    status: LeadStatus
    contractor: Optional[Contractor] = None
    lead_fee: Optional[float] = None
    whisper_message: Optional[str] = None
    reason: Optional[str] = None
    candidate_queue: list[Contractor] = field(default_factory=list)


@dataclass
class CallSettlement:
    lead_id: str
    contractor_id: str
    call_duration_seconds: int
    call_status: str  # "completed", "no-answer", "busy", "failed"


@dataclass
class SettlementResult:
    lead_id: str
    charged: bool
    amount_cents: Optional[int]
    reason: str


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

# Pricing model: the contractor pays exactly the bid they set, per connected
# lead -- no surge multiplier, no urgency bump, no floor/ceiling. The number a
# contractor picks in the partner portal ($35-$150) is precisely what they are
# charged, keeping the portal's "flat fee you pay per connected call" literal.
MIN_BILLABLE_DURATION_SECONDS = 60


def compute_lead_fee(base_bid: float) -> float:
    """The lead fee is exactly the contractor's bid, rounded to cents."""
    return round(base_bid, 2)


def contractor_whisper(trade: str, zip_code: str, urgency: str, fee: float) -> str:
    """The warm-transfer whisper spoken only to the contractor: the job, the
    area, the pay, and how to decline. Shared by the initial match and the
    failover endpoint so both read identically. The ZIP is spoken digit-by-digit
    so TTS says "seven seven zero zero two", not "seventy-seven thousand two"."""
    spoken_zip = " ".join(str(zip_code))
    return (
        f"New Dialpatch {trade} lead in ZIP {spoken_zip}, {urgency} urgency. "
        f"Lead fee ${fee:.2f}, charged only if you take the call and stay on the line. "
        f"Hang up to pass this lead to another contractor."
    )


# ---------------------------------------------------------------------------
# Matching / ranking
# ---------------------------------------------------------------------------

# Safety-critical keywords the voice agent should already intercept before
# ever calling this engine -- but we defense-in-depth check here too, since
# the backend should never assume the upstream agent got it right.
SAFETY_ESCALATION_TERMS = {
    "gas smell", "gas leak", "sparks", "fire", "smoke", "explosion",
    "chest pain", "unconscious", "not breathing", "carbon monoxide",
}


class DispatchEngine:
    def __init__(self, contractors: list[Contractor]):
        self._contractors = {c.id: c for c in contractors}

    def list_contractors(self) -> list[Contractor]:
        return list(self._contractors.values())

    def add_contractor(self, contractor: Contractor) -> None:
        self._contractors[contractor.id] = contractor

    def _eligible(self, lead: LeadRequest) -> list[Contractor]:
        return [
            c for c in self._contractors.values()
            if c.is_active
            and c.trade == lead.trade
            and lead.zip_code in c.coverage_zips
            and c.has_valid_billing_mandate
            and c.consecutive_no_answers < c.max_consecutive_no_answers
        ]

    def match(self, lead: LeadRequest, safety_flag_text: str = "") -> MatchResult:
        lead_id = str(uuid.uuid4())

        # Defense-in-depth safety check -- never match/bill a life-safety call.
        lowered = safety_flag_text.lower()
        if any(term in lowered for term in SAFETY_ESCALATION_TERMS):
            return MatchResult(
                lead_id=lead_id,
                status=LeadStatus.FLAGGED_SAFETY,
                reason="Safety keyword detected. Caller should be instructed to evacuate/call 911. No contractor match or billing occurs.",
            )

        # No caller-consent gate: the caller is never charged (the contractor
        # pays the lead fee), so there's nothing for them to consent to. The
        # agent's opening line still discloses that this is a paid referral
        # service and not 911 -- that disclosure stays; only the explicit
        # "do you confirm?" gate is dropped. disclosure_acknowledged is still
        # recorded on the lead for auditing, just no longer required to match.

        candidates = self._eligible(lead)
        if not candidates:
            return MatchResult(
                lead_id=lead_id,
                status=LeadStatus.NO_MATCH,
                reason=f"No active, billing-eligible {lead.trade.value} contractor covers ZIP {lead.zip_code}.",
            )

        # Highest bidder for this ZIP wins the lead (candidates are already
        # filtered to those covering it). The contractor willing to pay the most
        # per lead is connected first; reputation only breaks exact-bid ties.
        ranked = sorted(
            candidates,
            key=lambda c: (c.base_bid, c.reputation_score),
            reverse=True,
        )
        top = ranked[0]
        fee = compute_lead_fee(top.base_bid)

        # Spoken to the contractor as a warm-transfer whisper (they hear it,
        # the caller doesn't) -- states the job and the pay. The transfer
        # bridges automatically, so there is no "press 1" keypad step.
        whisper = contractor_whisper(lead.trade.value, lead.zip_code, lead.urgency.value, fee)

        return MatchResult(
            lead_id=lead_id,
            status=LeadStatus.MATCHED,
            contractor=top,
            lead_fee=fee,
            whisper_message=whisper,
            candidate_queue=ranked[1:],  # failover order if #1 doesn't answer
        )

    def next_in_failover(self, result: MatchResult) -> Optional[Contractor]:
        """Pop next contractor from the failover queue after a no-answer/reject."""
        if not result.candidate_queue:
            return None
        return result.candidate_queue.pop(0)

    def record_no_answer(self, contractor: Contractor) -> None:
        contractor.consecutive_no_answers += 1
        if contractor.consecutive_no_answers >= contractor.max_consecutive_no_answers:
            contractor.is_active = False  # auto-pause a contractor who keeps missing calls

    def record_answer(self, contractor: Contractor) -> None:
        contractor.consecutive_no_answers = 0


# ---------------------------------------------------------------------------
# Settlement (billing)
# ---------------------------------------------------------------------------

def settle_call(settlement: CallSettlement, lead_fee: float, stripe_charge_fn) -> SettlementResult:
    """
    Decide whether/how much to charge, then delegate the actual charge to
    stripe_charge_fn(amount_cents) -- injected so this stays testable without
    hitting the real Stripe API.

    Key change from the blueprint: billing is gated on *both* duration AND
    call_status == "completed", and never fires on anything else, closing
    the ambiguity in the original webhook handler.
    """
    if settlement.call_status != "completed":
        return SettlementResult(settlement.lead_id, charged=False, amount_cents=None,
                                 reason=f"Call did not complete (status={settlement.call_status}); not billed.")

    if settlement.call_duration_seconds < MIN_BILLABLE_DURATION_SECONDS:
        return SettlementResult(settlement.lead_id, charged=False, amount_cents=None,
                                 reason=f"Duration {settlement.call_duration_seconds}s below "
                                        f"{MIN_BILLABLE_DURATION_SECONDS}s billable threshold.")

    amount_cents = int(round(lead_fee * 100))
    stripe_charge_fn(amount_cents)
    return SettlementResult(settlement.lead_id, charged=True, amount_cents=amount_cents,
                             reason="Duration threshold met; charge submitted.")
