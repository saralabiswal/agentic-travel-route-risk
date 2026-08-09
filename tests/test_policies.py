from domain.policies import CorporateTravelPolicy, evaluate_recovery_policy
from domain.recovery import RecoveryCandidate


def test_policy_rejects_over_budget_recovery():
    candidate = RecoveryCandidate(
        candidate_id="too-expensive",
        available=True,
        policy_compliant=True,
        accessibility_compliant=True,
        feasible=True,
        arrival_delay_minutes=10,
        incremental_cost=600,
        connection_minutes=60,
    )
    result = evaluate_recovery_policy(
        candidate, CorporateTravelPolicy(maximum_incremental_cost=500)
    )
    assert result.eligible is False
    assert result.reasons == ["cost_exceeds_policy"]
