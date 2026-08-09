from uuid import uuid4

import pytest

from domain.models import RiskFactors, Severity
from domain.risk_engine import calculate_risk, severity_for_score


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, Severity.LOW),
        (24.99, Severity.LOW),
        (25, Severity.WATCH),
        (50, Severity.HIGH),
        (75, Severity.CRITICAL),
    ],
)
def test_severity_thresholds(score, expected):
    assert severity_for_score(score) == expected


def test_calculate_risk_is_explainable_and_deterministic():
    assessment = calculate_risk(
        trip_id=uuid4(),
        factors=RiskFactors(
            flight_disruption=80,
            connection_fragility=50,
            airport_weather=40,
            ground_route_disruption=20,
            destination_advisory=10,
            traveler_trip_criticality=100,
        ),
        evidence_ids=[],
    )
    assert assessment.risk_score == 54
    assert assessment.severity == Severity.HIGH
    assert assessment.factor_contributions["flight_disruption"] == 28
