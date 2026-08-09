"""Read-only alternative-flight fixture service; never creates or changes a booking."""

from __future__ import annotations

from domain.recovery import RecoveryCandidate


def fixture_alternatives() -> list[RecoveryCandidate]:
    return [
        RecoveryCandidate(
            candidate_id="fixture-alt-1",
            available=True,
            policy_compliant=True,
            accessibility_compliant=True,
            feasible=True,
            arrival_delay_minutes=45,
            incremental_cost=120,
            connection_minutes=75,
        ),
        RecoveryCandidate(
            candidate_id="fixture-alt-2",
            available=True,
            policy_compliant=True,
            accessibility_compliant=True,
            feasible=True,
            arrival_delay_minutes=90,
            incremental_cost=20,
            connection_minutes=55,
        ),
    ]
