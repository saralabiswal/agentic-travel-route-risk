from datetime import datetime

import pytest

from domain.models import FlightSegmentCreate, Trip, TripCriticality
from tools.evaluation import (
    EVALUATION_SUITE_VERSION,
    GOLDEN_SCENARIOS,
    evaluate_golden_scenario,
    evaluate_golden_suite,
    recommendation_is_grounded,
    recommendation_is_safe,
)
from tools.evidence import DemoScenario


@pytest.fixture
def golden_trip() -> Trip:
    return Trip(
        tenant_id="acme",
        traveler_id="traveler-1",
        trip_criticality=TripCriticality.BUSINESS_CRITICAL,
        ground_origin="1 Market St, San Francisco, CA",
        destination_country="US",
        segments=[
            FlightSegmentCreate(
                segment_id="sfo-den-1",
                carrier_code="UA",
                flight_number="123",
                departure_airport="SFO",
                arrival_airport="DEN",
                scheduled_departure_at=datetime.fromisoformat("2026-08-01T15:00:00+00:00"),
                scheduled_arrival_at=datetime.fromisoformat("2026-08-01T18:30:00+00:00"),
            )
        ],
    )


@pytest.mark.parametrize(
    ("scenario", "score", "severity", "disposition"),
    [
        (DemoScenario.NORMAL, 12.25, "low", "monitor"),
        (DemoScenario.DISRUPTION, 70.5, "high", "investigate"),
        (DemoScenario.SOURCE_OUTAGE, 9.5, "low", "needs_human_review"),
    ],
)
def test_golden_provider_policy_and_routing_scenarios(
    golden_trip, scenario, score, severity, disposition
):
    result = evaluate_golden_scenario(golden_trip, scenario)
    assert result.provider_contract_valid
    assert result.risk_score == score
    assert result.severity == severity
    assert result.disposition.value == disposition


def test_model_evaluation_rejects_ungrounded_citations():
    assert recommendation_is_grounded({"evidence_ids": ["e-1"]}, {"e-1"})
    assert not recommendation_is_grounded({"evidence_ids": ["unknown"]}, {"e-1"})
    assert not recommendation_is_grounded({"evidence_ids": []}, {"e-1"})


def test_versioned_release_suite_has_prd_scale_and_expected_routing(golden_trip):
    assert EVALUATION_SUITE_VERSION
    assert 40 <= len(GOLDEN_SCENARIOS) <= 60
    assert len({case.case_id for case in GOLDEN_SCENARIOS}) == len(GOLDEN_SCENARIOS)
    suite = evaluate_golden_suite(golden_trip)
    assert len(suite) == len(GOLDEN_SCENARIOS)
    assert all(result.disposition == case.expected_disposition for case, result in suite)


def test_model_evaluation_rejects_ungrounded_or_unapproved_alternatives():
    safe = {
        "evidence_ids": ["e-1"],
        "ranked_alternative_ids": ["alt-1"],
        "requires_human_approval": True,
    }
    assert recommendation_is_safe(safe, evidence_ids={"e-1"}, alternative_ids={"alt-1"})
    assert not recommendation_is_safe(
        {**safe, "ranked_alternative_ids": ["unknown"]},
        evidence_ids={"e-1"},
        alternative_ids={"alt-1"},
    )
    assert not recommendation_is_safe(
        {**safe, "requires_human_approval": False},
        evidence_ids={"e-1"},
        alternative_ids={"alt-1"},
    )
