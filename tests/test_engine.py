"""
Unit tests for the core dispatch engine.

These cover the money- and safety-critical logic in isolation:
pricing/surge, contractor eligibility and ranking, failover, auto-pause,
the safety-escalation and consent guard rails, and settlement gating.

No FastAPI/SQLAlchemy/network needed -- pure stdlib, matching the engine's
deliberate framework-independence.
"""

from datetime import datetime, timezone

import pytest

from core.engine import (
    Contractor, LeadRequest, MatchResult, CallSettlement,
    DispatchEngine, Trade, UrgencyLevel, LeadStatus,
    compute_lead_fee, is_surge_window, settle_call,
    SURGE_MULTIPLIER, BASE_LEAD_FEE_FLOOR, BASE_LEAD_FEE_CEILING,
    MIN_BILLABLE_DURATION_SECONDS,
)


# --- fixtures / helpers ----------------------------------------------------

def _contractor(**overrides):
    base = dict(
        id="c_1", name="Apex Plumbing", phone_number="+17135550199",
        trade=Trade.PLUMBING, coverage_zips={"77001", "77002"},
        is_active=True, base_bid=65.0, stripe_customer_id="cus_1",
        reputation_score=4.9,
    )
    base.update(overrides)
    return Contractor(**base)


def _lead(**overrides):
    base = dict(
        caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
        urgency=UrgencyLevel.HIGH, street_address="123 Main St",
        disclosure_acknowledged=True,
    )
    base.update(overrides)
    return LeadRequest(**base)


# A daytime hour is never in the 22:00-06:00 surge window; a late-night one is.
DAY = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 8, 25, 23, 0, tzinfo=timezone.utc)


class _Recorder:
    """Stand-in for the Stripe charge fn: records amounts instead of charging."""
    def __init__(self):
        self.calls = []

    def __call__(self, amount_cents):
        self.calls.append(amount_cents)


# --- surge window ----------------------------------------------------------

@pytest.mark.parametrize("hour,expected", [
    (0, True), (5, True), (6, False), (12, False),
    (21, False), (22, True), (23, True),
])
def test_is_surge_window_boundaries(hour, expected):
    now = datetime(2026, 8, 25, hour, 0, tzinfo=timezone.utc)
    assert is_surge_window(now) is expected


# --- pricing ---------------------------------------------------------------

def test_lead_fee_daytime_standard_is_base_bid():
    fee = compute_lead_fee(65.0, UrgencyLevel.STANDARD, now=DAY)
    assert fee == 65.0


def test_lead_fee_surge_multiplier_applied_at_night():
    day = compute_lead_fee(65.0, UrgencyLevel.STANDARD, now=DAY)
    night = compute_lead_fee(65.0, UrgencyLevel.STANDARD, now=NIGHT)
    assert night == pytest.approx(day * SURGE_MULTIPLIER, abs=0.01)


def test_lead_fee_critical_urgency_bump():
    standard = compute_lead_fee(65.0, UrgencyLevel.STANDARD, now=DAY)
    critical = compute_lead_fee(65.0, UrgencyLevel.CRITICAL, now=DAY)
    assert critical == pytest.approx(standard * 1.10, abs=0.01)


def test_lead_fee_clamped_to_floor():
    # A very low bid can never produce a fee below the floor.
    assert compute_lead_fee(10.0, UrgencyLevel.STANDARD, now=DAY) == BASE_LEAD_FEE_FLOOR


def test_lead_fee_clamped_to_ceiling():
    # A very high bid plus surge plus critical still caps at the ceiling.
    fee = compute_lead_fee(500.0, UrgencyLevel.CRITICAL, now=NIGHT)
    assert fee == BASE_LEAD_FEE_CEILING


# --- matching: guard rails -------------------------------------------------

def test_match_safety_keyword_never_matches_or_bills():
    engine = DispatchEngine([_contractor()])
    result = engine.match(_lead(), safety_flag_text="caller reports a strong gas leak")
    assert result.status == LeadStatus.FLAGGED_SAFETY
    assert result.contractor is None
    assert result.lead_fee is None


def test_match_safety_keyword_is_case_insensitive():
    engine = DispatchEngine([_contractor()])
    result = engine.match(_lead(), safety_flag_text="There is SMOKE everywhere")
    assert result.status == LeadStatus.FLAGGED_SAFETY


def test_match_requires_disclosure_acknowledgement():
    engine = DispatchEngine([_contractor()])
    result = engine.match(_lead(disclosure_acknowledged=False))
    assert result.status == LeadStatus.NO_MATCH
    assert result.contractor is None


def test_match_no_match_when_no_coverage():
    engine = DispatchEngine([_contractor(coverage_zips={"77001"})])
    result = engine.match(_lead(zip_code="99999"))
    assert result.status == LeadStatus.NO_MATCH


def test_match_no_match_when_wrong_trade():
    engine = DispatchEngine([_contractor(trade=Trade.HVAC)])
    result = engine.match(_lead(trade=Trade.PLUMBING))
    assert result.status == LeadStatus.NO_MATCH


# --- matching: eligibility filters -----------------------------------------

def test_inactive_contractor_excluded():
    engine = DispatchEngine([_contractor(is_active=False)])
    assert engine.match(_lead()).status == LeadStatus.NO_MATCH


def test_contractor_without_billing_mandate_excluded():
    engine = DispatchEngine([_contractor(has_valid_billing_mandate=False)])
    assert engine.match(_lead()).status == LeadStatus.NO_MATCH


def test_contractor_at_no_answer_cap_excluded():
    engine = DispatchEngine([_contractor(consecutive_no_answers=3, max_consecutive_no_answers=3)])
    assert engine.match(_lead()).status == LeadStatus.NO_MATCH


# --- matching: ranking + failover ------------------------------------------

def test_match_ranks_by_bid_and_reputation_blend():
    high = _contractor(id="c_high", base_bid=65.0, reputation_score=4.9)
    low = _contractor(id="c_low", base_bid=50.0, reputation_score=4.5)
    engine = DispatchEngine([low, high])  # order shouldn't matter
    result = engine.match(_lead())
    assert result.status == LeadStatus.MATCHED
    assert result.contractor.id == "c_high"
    assert [c.id for c in result.candidate_queue] == ["c_low"]


def test_billing_mandate_beats_higher_bid():
    # A higher-bid/higher-rep contractor with no mandate must lose to an eligible one.
    no_mandate = _contractor(id="c_nm", base_bid=80.0, reputation_score=5.0,
                             has_valid_billing_mandate=False)
    eligible = _contractor(id="c_ok", base_bid=50.0, reputation_score=4.5)
    engine = DispatchEngine([no_mandate, eligible])
    result = engine.match(_lead())
    assert result.contractor.id == "c_ok"


def test_next_in_failover_pops_queue_then_none():
    engine = DispatchEngine([
        _contractor(id="c_1", base_bid=65.0, reputation_score=4.9),
        _contractor(id="c_2", base_bid=50.0, reputation_score=4.5),
    ])
    result = engine.match(_lead())
    nxt = engine.next_in_failover(result)
    assert nxt.id == "c_2"
    assert engine.next_in_failover(result) is None  # queue now empty


def test_matched_result_has_whisper_and_fee():
    engine = DispatchEngine([_contractor()])
    result = engine.match(_lead(zip_code="77002"), now=DAY)
    assert result.lead_fee is not None
    assert "77002" in result.whisper_message
    assert "Press 1" in result.whisper_message


# --- no-answer accounting / auto-pause -------------------------------------

def test_record_no_answer_auto_pauses_at_cap():
    engine = DispatchEngine([])
    c = _contractor(max_consecutive_no_answers=3)
    for _ in range(3):
        engine.record_no_answer(c)
    assert c.consecutive_no_answers == 3
    assert c.is_active is False


def test_record_answer_resets_no_answer_streak():
    engine = DispatchEngine([])
    c = _contractor(consecutive_no_answers=2)
    engine.record_answer(c)
    assert c.consecutive_no_answers == 0


# --- settlement ------------------------------------------------------------

def test_settle_charges_completed_call_over_threshold():
    rec = _Recorder()
    s = CallSettlement(lead_id="l1", contractor_id="c1",
                       call_duration_seconds=95, call_status="completed")
    result = settle_call(s, lead_fee=71.50, stripe_charge_fn=rec)
    assert result.charged is True
    assert result.amount_cents == 7150
    assert rec.calls == [7150]


def test_settle_skips_short_call():
    rec = _Recorder()
    s = CallSettlement(lead_id="l1", contractor_id="c1",
                       call_duration_seconds=MIN_BILLABLE_DURATION_SECONDS - 1,
                       call_status="completed")
    result = settle_call(s, lead_fee=71.50, stripe_charge_fn=rec)
    assert result.charged is False
    assert result.amount_cents is None
    assert rec.calls == []  # Stripe never invoked


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed"])
def test_settle_skips_incomplete_call_regardless_of_duration(status):
    rec = _Recorder()
    s = CallSettlement(lead_id="l1", contractor_id="c1",
                       call_duration_seconds=600, call_status=status)
    result = settle_call(s, lead_fee=71.50, stripe_charge_fn=rec)
    assert result.charged is False
    assert rec.calls == []


def test_settle_converts_dollar_fee_to_integer_cents():
    # Real fees are 2-decimal (compute_lead_fee rounds to 2 places); confirm the
    # dollars->cents conversion is exact for such a value.
    rec = _Recorder()
    s = CallSettlement(lead_id="l1", contractor_id="c1",
                       call_duration_seconds=95, call_status="completed")
    result = settle_call(s, lead_fee=88.75, stripe_charge_fn=rec)
    assert result.amount_cents == 8875
    assert rec.calls == [8875]
