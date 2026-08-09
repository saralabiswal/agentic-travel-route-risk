"""Deterministic recovery eligibility and ranking; never performs booking actions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class RecoveryCandidateState(StrEnum):
    """The current, observable lifecycle state for one recovered itinerary."""

    UNAVAILABLE = "unavailable"
    POLICY_INELIGIBLE = "policy_ineligible"
    INELIGIBLE = "ineligible"
    ELIGIBLE = "eligible"
    OFFERED = "offered"
    VIEWED = "viewed"
    SELECTED = "selected"
    REJECTED = "rejected"
    COMPLETED = "completed"


class RecoveryCandidateOutcomeState(StrEnum):
    """Append-only lifecycle events allowed after deterministic evaluation."""

    OFFERED = "offered"
    VIEWED = "viewed"
    SELECTED = "selected"
    REJECTED = "rejected"
    COMPLETED = "completed"


class RecoveryCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    available: bool
    policy_compliant: bool
    accessibility_compliant: bool
    feasible: bool
    arrival_delay_minutes: int = Field(ge=0)
    incremental_cost: float = Field(ge=0)
    connection_minutes: int = Field(ge=0)


class EvaluatedCandidate(RecoveryCandidate):
    eligible: bool
    exclusion_reasons: list[str]
    deterministic_recovery_score: float | None = Field(default=None, ge=0, le=100)
    normalized_candidate_features: dict[str, bool | int | float] = Field(default_factory=dict)
    feature_schema_version: str = "recovery-candidate-v1"
    displayed_position: int | None = Field(default=None, ge=1)
    ranker_score: float | None = None
    ranker_model_version: str | None = None
    lifecycle_state: RecoveryCandidateState = RecoveryCandidateState.ELIGIBLE


class RecoveryCandidateOutcomeUpdate(BaseModel):
    """A requested, validated outcome transition; the actor comes from authentication."""

    state: RecoveryCandidateOutcomeState
    reason: str | None = Field(default=None, max_length=1000)
    manager_override_reason: str | None = Field(default=None, max_length=1000)
    final_itinerary: dict[str, object] | None = None
    material_outcome: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_required_outcome_details(self) -> RecoveryCandidateOutcomeUpdate:
        if self.state == RecoveryCandidateOutcomeState.REJECTED and not self.reason:
            raise ValueError("a rejection reason is required")
        if self.manager_override_reason and self.state != RecoveryCandidateOutcomeState.SELECTED:
            raise ValueError("a manager override reason is valid only when selecting a candidate")
        if self.state == RecoveryCandidateOutcomeState.COMPLETED and not self.material_outcome:
            raise ValueError("a completed candidate requires a material outcome")
        return self


class RecoveryCandidateOutcome(RecoveryCandidateOutcomeUpdate):
    """An append-only observation. It never changes candidate eligibility or ranking."""

    outcome_id: UUID = Field(default_factory=uuid4)
    candidate_set_id: UUID
    incident_id: UUID
    candidate_id: str = Field(min_length=1)
    actor_id: str | None = Field(default=None, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecoveryCandidateSet(BaseModel):
    candidate_set_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    score_version: str = "deterministic-v1"
    policy_version: str = "mvp-v1"
    candidates: list[EvaluatedCandidate]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # This is an API projection of append-only outcome records; it is not written
    # into the immutable candidate-set snapshot.
    outcomes: list[RecoveryCandidateOutcome] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    manager_override_reason: str | None = None


class RecoveryCandidateSetCreate(BaseModel):
    """Raw provider candidates. Eligibility and scores are server-owned facts."""

    candidates: list[RecoveryCandidate] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def candidate_ids_are_unique(self) -> RecoveryCandidateSetCreate:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_id values must be unique within a candidate set")
        return self


def evaluate_candidate(
    candidate: RecoveryCandidate, *, minimum_connection_minutes: int
) -> EvaluatedCandidate:
    reasons: list[str] = []
    if not candidate.available:
        reasons.append("unavailable")
    if not candidate.feasible:
        reasons.append("itinerary_infeasible")
    if not candidate.policy_compliant:
        reasons.append("policy_ineligible")
    if not candidate.accessibility_compliant:
        reasons.append("accessibility_ineligible")
    if candidate.connection_minutes < minimum_connection_minutes:
        reasons.append("connection_below_minimum")
    eligible = not reasons
    score = None
    if eligible:
        score = round(
            max(0, 100 - candidate.arrival_delay_minutes * 0.5 - candidate.incremental_cost * 0.1),
            2,
        )
    return EvaluatedCandidate(
        **candidate.model_dump(),
        eligible=eligible,
        exclusion_reasons=reasons,
        deterministic_recovery_score=score,
        normalized_candidate_features={
            "available": candidate.available,
            "policy_compliant": candidate.policy_compliant,
            "accessibility_compliant": candidate.accessibility_compliant,
            "feasible": candidate.feasible,
            "arrival_delay_minutes": candidate.arrival_delay_minutes,
            "incremental_cost": candidate.incremental_cost,
            "connection_minutes": candidate.connection_minutes,
        },
        lifecycle_state=(
            RecoveryCandidateState.UNAVAILABLE
            if not candidate.available
            else RecoveryCandidateState.POLICY_INELIGIBLE
            if not candidate.policy_compliant
            else RecoveryCandidateState.INELIGIBLE
            if not eligible
            else RecoveryCandidateState.ELIGIBLE
        ),
    )


def rank_eligible(candidates: list[EvaluatedCandidate]) -> list[EvaluatedCandidate]:
    """Return every eligible candidate in deterministic score order and display position."""
    ranked = sorted(
        (candidate for candidate in candidates if candidate.eligible),
        key=lambda candidate: (candidate.deterministic_recovery_score or 0, candidate.candidate_id),
        reverse=True,
    )
    return [
        candidate.model_copy(update={"displayed_position": position})
        for position, candidate in enumerate(ranked, start=1)
    ]


def project_candidate_set_outcomes(
    candidate_set: RecoveryCandidateSet, outcomes: list[RecoveryCandidateOutcome]
) -> RecoveryCandidateSet:
    """Build a current-state view without modifying the immutable ranked snapshot."""
    by_candidate: dict[str, RecoveryCandidateOutcome] = {}
    for outcome in sorted(outcomes, key=lambda item: (item.occurred_at, str(item.outcome_id))):
        by_candidate[outcome.candidate_id] = outcome

    candidates = [
        candidate.model_copy(
            update={
                "lifecycle_state": RecoveryCandidateState(
                    by_candidate[candidate.candidate_id].state.value
                )
            }
        )
        if candidate.candidate_id in by_candidate
        else candidate
        for candidate in candidate_set.candidates
    ]
    selections = [
        outcome
        for outcome in outcomes
        if outcome.state == RecoveryCandidateOutcomeState.SELECTED
    ]
    latest_selection = (
        max(selections, key=lambda item: (item.occurred_at, str(item.outcome_id)))
        if selections
        else None
    )
    return candidate_set.model_copy(
        update={
            "candidates": candidates,
            "outcomes": sorted(outcomes, key=lambda item: (item.occurred_at, str(item.outcome_id))),
            "selected_candidate_id": latest_selection.candidate_id if latest_selection else None,
            "manager_override_reason": (
                latest_selection.manager_override_reason if latest_selection else None
            ),
        }
    )


def transition_allowed(
    *, current_state: RecoveryCandidateState, target_state: RecoveryCandidateOutcomeState
) -> bool:
    """Keep lifecycle observations ordered and make duplicate outcomes visible."""
    allowed = {
        RecoveryCandidateState.ELIGIBLE: {RecoveryCandidateOutcomeState.OFFERED},
        RecoveryCandidateState.OFFERED: {
            RecoveryCandidateOutcomeState.VIEWED,
            RecoveryCandidateOutcomeState.SELECTED,
            RecoveryCandidateOutcomeState.REJECTED,
        },
        RecoveryCandidateState.VIEWED: {
            RecoveryCandidateOutcomeState.SELECTED,
            RecoveryCandidateOutcomeState.REJECTED,
        },
        RecoveryCandidateState.SELECTED: {RecoveryCandidateOutcomeState.COMPLETED},
    }
    return target_state in allowed.get(current_state, set())
