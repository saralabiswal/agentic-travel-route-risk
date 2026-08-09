from domain.policies import CorporateTravelPolicy, evaluate_policy_eligible_candidate
from domain.recovery import RecoveryCandidate, evaluate_candidate, rank_eligible
from workers.monitor_due_trips import assessment_due_idempotency_key


def test_recovery_hard_constraints_apply_before_ranking():
    candidates = [
        evaluate_candidate(
            RecoveryCandidate(
                candidate_id="eligible",
                available=True,
                policy_compliant=True,
                accessibility_compliant=True,
                feasible=True,
                arrival_delay_minutes=20,
                incremental_cost=50,
                connection_minutes=60,
            ),
            minimum_connection_minutes=45,
        ),
        evaluate_candidate(
            RecoveryCandidate(
                candidate_id="blocked",
                available=True,
                policy_compliant=False,
                accessibility_compliant=True,
                feasible=True,
                arrival_delay_minutes=0,
                incremental_cost=0,
                connection_minutes=90,
            ),
            minimum_connection_minutes=45,
        ),
    ]
    assert candidates[1].exclusion_reasons == ["policy_ineligible"]
    ranked = rank_eligible(candidates)
    assert [item.candidate_id for item in ranked] == ["eligible"]
    assert ranked[0].displayed_position == 1


def test_server_policy_can_exclude_a_provider_available_candidate_before_scoring():
    candidate = RecoveryCandidate(
        candidate_id="over-budget",
        available=True,
        policy_compliant=True,
        accessibility_compliant=True,
        feasible=True,
        arrival_delay_minutes=0,
        incremental_cost=501,
        connection_minutes=90,
    )
    evaluated = evaluate_policy_eligible_candidate(
        candidate, CorporateTravelPolicy(maximum_incremental_cost=500)
    )
    assert evaluated.eligible is False
    assert evaluated.lifecycle_state == "policy_ineligible"
    assert "cost_exceeds_policy" in evaluated.exclusion_reasons
    assert evaluated.deterministic_recovery_score is None


def test_due_worker_key_is_replay_safe():
    assert (
        assessment_due_idempotency_key(trip_id="trip-1", window="T-24")
        == "assessment-due:trip-1:T-24"
    )
