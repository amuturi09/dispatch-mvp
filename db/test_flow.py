"""
End-to-end simulation of the dispatch flow, using only the stdlib + core engine.
Run: python3 test_flow.py

This exercises: seed data -> matching -> failover -> whisper -> settlement,
plus the safety-escalation and no-consent guard rails.
"""

from core.engine import (
    Contractor, LeadRequest, DispatchEngine, Trade, UrgencyLevel,
    LeadStatus, CallSettlement, settle_call,
)


def seed_contractors():
    return [
        Contractor(id="c_1", name="Apex 24/7 Plumbing", phone_number="+17135550199",
                   trade=Trade.PLUMBING, coverage_zips={"77001", "77002", "77005"},
                   is_active=True, base_bid=65.0, stripe_customer_id="cus_1", reputation_score=4.9),
        Contractor(id="c_2", name="Metro Emergency Rooter", phone_number="+17135550244",
                   trade=Trade.PLUMBING, coverage_zips={"77002", "77007", "77008"},
                   is_active=True, base_bid=50.0, stripe_customer_id="cus_2", reputation_score=4.5),
        Contractor(id="c_3", name="NoMandate Plumbing", phone_number="+17135550300",
                   trade=Trade.PLUMBING, coverage_zips={"77002"},
                   is_active=True, base_bid=80.0, stripe_customer_id="cus_3", reputation_score=5.0,
                   has_valid_billing_mandate=False),  # should be excluded despite high bid/reputation
    ]


def mock_stripe_charge(amount_cents):
    print(f"    [mock stripe] PaymentIntent created for ${amount_cents/100:.2f}")


def section(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def scenario_normal_match():
    section("SCENARIO 1: Normal match + successful bridge + billing")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(
        caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
        urgency=UrgencyLevel.CRITICAL, street_address="123 Main St, Houston, TX",
        disclosure_acknowledged=True,
    )
    result = engine.match(lead)
    print(f"Status: {result.status}")
    assert result.status == LeadStatus.MATCHED
    print(f"Matched contractor: {result.contractor.name} (bid=${result.contractor.base_bid}, "
          f"rep={result.contractor.reputation_score}) -- beat higher-bid contractor with no billing mandate")
    print(f"Lead fee (post-pricing rules): ${result.lead_fee:.2f}")
    print(f"Whisper script: \"{result.whisper_message}\"")

    settlement = CallSettlement(lead_id=result.lead_id, contractor_id=result.contractor.id,
                                 call_duration_seconds=95, call_status="completed")
    outcome = settle_call(settlement, result.lead_fee, mock_stripe_charge)
    print(f"Settlement: charged={outcome.charged}, amount=${(outcome.amount_cents or 0)/100:.2f} -- {outcome.reason}")
    assert outcome.charged


def scenario_failover():
    section("SCENARIO 2: First contractor doesn't answer -> failover to #2")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(
        caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
        urgency=UrgencyLevel.HIGH, street_address="456 Oak St, Houston, TX",
        disclosure_acknowledged=True,
    )
    result = engine.match(lead)
    print(f"Primary match: {result.contractor.name}")
    print("...Twilio dials primary, 4 rings, no answer...")
    engine.record_no_answer(result.contractor)
    next_contractor = engine.next_in_failover(result)
    print(f"Failover to: {next_contractor.name}")
    assert next_contractor.id == "c_2"
    print("...contractor #2 answers, presses 1, bridges...")
    engine.record_answer(next_contractor)


def scenario_short_call_not_billed():
    section("SCENARIO 3: Call under 60s -- must NOT bill")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
                        urgency=UrgencyLevel.STANDARD, street_address="789 Pine St",
                        disclosure_acknowledged=True)
    result = engine.match(lead)
    settlement = CallSettlement(lead_id=result.lead_id, contractor_id=result.contractor.id,
                                 call_duration_seconds=22, call_status="completed")
    outcome = settle_call(settlement, result.lead_fee, mock_stripe_charge)
    print(f"Settlement: charged={outcome.charged} -- {outcome.reason}")
    assert not outcome.charged


def scenario_no_consent_blocks_match():
    section("SCENARIO 4: Caller never acknowledged paid-referral disclosure -- must NOT match")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
                        urgency=UrgencyLevel.STANDARD, street_address="1 No Consent Ave",
                        disclosure_acknowledged=False)
    result = engine.match(lead)
    print(f"Status: {result.status} -- {result.reason}")
    assert result.status == LeadStatus.NO_MATCH


def scenario_safety_escalation():
    section("SCENARIO 5: Caller mentions gas smell -- must escalate, never match/bill")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(caller_phone="+17135551234", trade=Trade.PLUMBING, zip_code="77002",
                        urgency=UrgencyLevel.CRITICAL, street_address="2 Danger Rd",
                        disclosure_acknowledged=True)
    result = engine.match(lead, safety_flag_text="caller reports strong gas smell in the kitchen")
    print(f"Status: {result.status} -- {result.reason}")
    assert result.status == LeadStatus.FLAGGED_SAFETY
    assert result.contractor is None


def scenario_no_coverage():
    section("SCENARIO 6: No contractor covers this ZIP -- graceful no-match")
    engine = DispatchEngine(seed_contractors())
    lead = LeadRequest(caller_phone="+17135551234", trade=Trade.HVAC, zip_code="99999",
                        urgency=UrgencyLevel.HIGH, street_address="3 Nowhere Ln",
                        disclosure_acknowledged=True)
    result = engine.match(lead)
    print(f"Status: {result.status} -- {result.reason}")
    assert result.status == LeadStatus.NO_MATCH


def scenario_contractor_auto_pause():
    section("SCENARIO 7: Contractor auto-paused after repeated no-answers")
    engine = DispatchEngine(seed_contractors())
    c1 = engine.list_contractors()[0]
    for i in range(3):
        engine.record_no_answer(c1)
        print(f"  no-answer #{c1.consecutive_no_answers}, active={c1.is_active}")
    assert c1.is_active is False
    print(f"{c1.name} auto-paused after {c1.max_consecutive_no_answers} consecutive misses.")


if __name__ == "__main__":
    scenario_normal_match()
    scenario_failover()
    scenario_short_call_not_billed()
    scenario_no_consent_blocks_match()
    scenario_safety_escalation()
    scenario_no_coverage()
    scenario_contractor_auto_pause()
    print("\n" + "="*70)
    print("ALL SCENARIOS PASSED")
    print("="*70)
