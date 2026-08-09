"""Structured corporate recovery-policy checks; policy is authoritative over ranking."""

from __future__ import annotations

from pydantic import BaseModel, Field

from domain.recovery import (
    EvaluatedCandidate,
    RecoveryCandidate,
    RecoveryCandidateState,
    evaluate_candidate,
)


class CorporateTravelPolicy(BaseModel):
    version: str = "mvp-v1"
    maximum_incremental_cost: float = Field(default=500, ge=0)
    minimum_connection_minutes: int = Field(default=45, ge=0)
    allow_overnight_connections: bool = False


class PolicyDecision(BaseModel):
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    policy_version: str


def evaluate_recovery_policy(
    candidate: RecoveryCandidate, policy: CorporateTravelPolicy
) -> PolicyDecision:
    reasons: list[str] = []
    if candidate.incremental_cost > policy.maximum_incremental_cost:
        reasons.append("cost_exceeds_policy")
    if candidate.connection_minutes < policy.minimum_connection_minutes:
        reasons.append("connection_below_policy_minimum")
    return PolicyDecision(eligible=not reasons, reasons=reasons, policy_version=policy.version)


def evaluate_policy_eligible_candidate(
    candidate: RecoveryCandidate, policy: CorporateTravelPolicy
) -> EvaluatedCandidate:
    """Apply tenant policy before the deterministic score can be exposed or ranked."""
    policy_decision = evaluate_recovery_policy(candidate, policy)
    evaluated = evaluate_candidate(
        candidate.model_copy(
            update={"policy_compliant": candidate.policy_compliant and policy_decision.eligible}
        ),
        minimum_connection_minutes=policy.minimum_connection_minutes,
    )
    reasons = list(dict.fromkeys([*evaluated.exclusion_reasons, *policy_decision.reasons]))
    if not policy_decision.eligible:
        return evaluated.model_copy(
            update={
                "eligible": False,
                "exclusion_reasons": reasons,
                "deterministic_recovery_score": None,
                "lifecycle_state": RecoveryCandidateState.POLICY_INELIGIBLE,
            }
        )
    return evaluated.model_copy(update={"exclusion_reasons": reasons})


def apply_traveler_minimum_connection(
    candidate: EvaluatedCandidate, *, minimum_connection_minutes: int | None
) -> EvaluatedCandidate:
    """Apply a confirmed traveler constraint without relaxing corporate policy."""
    if (
        not minimum_connection_minutes
        or candidate.connection_minutes >= minimum_connection_minutes
        or not candidate.eligible
    ):
        return candidate
    return candidate.model_copy(
        update={
            "eligible": False,
            "exclusion_reasons": [
                *candidate.exclusion_reasons,
                "traveler_minimum_connection_not_met",
            ],
            "deterministic_recovery_score": None,
            "lifecycle_state": RecoveryCandidateState.INELIGIBLE,
        }
    )
