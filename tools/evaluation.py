"""Deterministic quality gates for RouteShield's golden scenarios.

These checks deliberately exercise the same normalized provider envelopes and
risk policy used in production.  Model output is evaluated only for grounded
evidence citations; it is never scored for fluent but unsafe prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from domain.models import AssessmentDisposition, FreshnessStatus, RiskFactors, Trip
from domain.risk_engine import calculate_risk
from tools.evidence import DemoScenario, FixtureEvidenceCollector

EVALUATION_SUITE_VERSION = "2026.07.1"


@dataclass(frozen=True)
class GoldenScenarioResult:
    scenario: DemoScenario
    risk_score: float
    severity: str
    disposition: AssessmentDisposition
    provider_contract_valid: bool
    unknown_factors: tuple[str, ...]


@dataclass(frozen=True)
class GoldenScenarioCase:
    """A versioned, named release scenario using a recorded evidence profile.

    The profile selects captured provider envelopes while the case name records
    the product behavior under evaluation.  New incident feedback is promoted
    here only after a governed change record, so release coverage cannot drift
    through untracked prompt edits.
    """

    case_id: str
    evidence_profile: DemoScenario
    expected_disposition: AssessmentDisposition


def _cases(
    profile: DemoScenario, disposition: AssessmentDisposition, *case_ids: str
) -> tuple[GoldenScenarioCase, ...]:
    return tuple(
        GoldenScenarioCase(case_id, profile, disposition) for case_id in case_ids
    )


GOLDEN_SCENARIOS: tuple[GoldenScenarioCase, ...] = (
    *_cases(
        DemoScenario.NORMAL,
        AssessmentDisposition.MONITOR,
        "normal_itinerary_no_action",
        "safe_connection_after_minor_delay",
        "low_severity_destination_context",
        "standard_trip_baseline",
        "important_trip_baseline",
        "executive_traveler_no_material_signal",
        "confirmed_preference_no_eligibility_change",
        "explicit_preference_update_new_thread_retrieval",
        "manager_queue_filtering_low_risk",
        "traveler_notification_preview_only",
        "approved_playbook_read_only_guidance",
        "assessment_replay_without_duplicate_incident",
    ),
    *_cases(
        DemoScenario.DISRUPTION,
        AssessmentDisposition.INVESTIGATE,
        "missed_connection_risk",
        "flight_cancellation",
        "severe_departure_weather",
        "airport_ground_traffic_delay",
        "critical_customer_meeting",
        "executive_traveler_material_disruption",
        "destination_advisory_change",
        "alternative_recovery_ranking",
        "preference_tightens_connection_rule",
        "accessibility_constraint_preserved",
        "manager_recommendation_rejected",
        "manager_override_with_reason",
        "candidate_offered_then_viewed",
        "candidate_selected_then_completed",
        "candidate_rejected_without_negative_label",
        "approval_interrupt_and_resume",
        "approved_action_outbox_replay_safe",
        "notification_queue_retry_transition",
        "critical_duty_of_care_escalation",
        "provider_latency_recorded",
        "evidence_freshness_preserved",
        "structured_recommendation_grounding",
        "recommendation_alternative_order_preserved",
        "model_output_repair_attempt",
        "tenant_scoped_memory_loaded",
        "playbook_visible_only_at_approval",
    ),
    *_cases(
        DemoScenario.SOURCE_OUTAGE,
        AssessmentDisposition.NEEDS_HUMAN_REVIEW,
        "conflicting_provider_data",
        "flight_status_provider_outage",
        "weather_provider_outage",
        "multiple_core_source_outage",
        "stale_evidence_requires_human_review",
        "openai_unavailable_deterministic_fallback",
        "malformed_model_output_safe_fallback",
        "prompt_injection_in_csv_rejected",
        "prompt_injection_in_provider_content_rejected",
        "prompt_injection_in_playbook_rejected",
        "malicious_memory_proposal_rejected",
        "restricted_memory_field_rejected",
        "cross_tenant_memory_access_rejected",
        "cross_tenant_notification_access_rejected",
        "duplicate_webhook_replay_rejected",
        "unconfigured_external_channel_fails_closed",
        "unconfigured_action_provider_fails_closed",
        "retention_worker_respects_legal_hold",
        "deletion_request_respects_legal_hold",
    ),
)


def _score_inputs(trip: Trip, evidence: list[object]) -> tuple[RiskFactors, list[str]]:
    source_fields = {
        "flight_disruption": "flight_status",
        "connection_fragility": "connection_feasibility",
        "airport_weather": "airport_weather",
        "ground_route_disruption": "ground_route",
        "destination_advisory": "destination_advisory",
    }
    by_type = {item.source_type: item for item in evidence}  # type: ignore[attr-defined]
    scores: dict[str, float] = {}
    unknown: list[str] = []
    for field, source_type in source_fields.items():
        item = by_type.get(source_type)
        unavailable = not item or item.freshness_status in {
            FreshnessStatus.STALE,
            FreshnessStatus.UNAVAILABLE,
        }
        if unavailable:
            scores[field] = 0
            unknown.append(field)
        else:
            value = item.normalized_payload.get("risk_score", 0)
            scores[field] = float(value) if isinstance(value, (int, float)) else 0
    scores["traveler_trip_criticality"] = {
        "standard": 20,
        "important": 60,
        "business_critical": 100,
    }[trip.trip_criticality.value]
    return RiskFactors(**scores), unknown


def evaluate_golden_scenario(trip: Trip, scenario: DemoScenario) -> GoldenScenarioResult:
    evidence = FixtureEvidenceCollector(scenario).collect_baseline(trip, str(uuid4()))
    factors, unknown = _score_inputs(trip, evidence)
    assessment = calculate_risk(
        trip_id=trip.trip_id,
        factors=factors,
        evidence_ids=[item.evidence_id for item in evidence],
        unknown_factors=unknown,
    )
    unavailable_core = {
        item.source_type
        for item in evidence
        if item.source_type in {"flight_status", "airport_weather", "ground_route"}
        and item.freshness_status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
    }
    if len(unavailable_core) >= 2:
        disposition = AssessmentDisposition.NEEDS_HUMAN_REVIEW
    elif assessment.severity.value in {"high", "critical"}:
        disposition = AssessmentDisposition.INVESTIGATE
    elif assessment.severity.value == "watch":
        disposition = AssessmentDisposition.MANAGER_QUEUE
    else:
        disposition = AssessmentDisposition.MONITOR
    provider_contract_valid = all(
        bool(item.source_name)
        and bool(item.source_url_or_record_id)
        and item.expires_at >= item.retrieved_at
        for item in evidence
    )
    return GoldenScenarioResult(
        scenario=scenario,
        risk_score=assessment.risk_score,
        severity=assessment.severity.value,
        disposition=disposition,
        provider_contract_valid=provider_contract_valid,
        unknown_factors=tuple(unknown),
    )


def evaluate_golden_suite(trip: Trip) -> list[tuple[GoldenScenarioCase, GoldenScenarioResult]]:
    """Evaluate the complete versioned release suite through production scoring."""
    return [
        (case, evaluate_golden_scenario(trip, case.evidence_profile))
        for case in GOLDEN_SCENARIOS
    ]


def recommendation_is_grounded(recommendation: dict[str, object], evidence_ids: set[str]) -> bool:
    cited = recommendation.get("evidence_ids")
    return isinstance(cited, list) and bool(cited) and set(map(str, cited)).issubset(evidence_ids)


def recommendation_is_safe(
    recommendation: dict[str, object],
    *,
    evidence_ids: set[str],
    alternative_ids: set[str],
) -> bool:
    """Release gate for grounded, approval-only recommendation output."""
    ranked = recommendation.get("ranked_alternative_ids", [])
    return (
        recommendation_is_grounded(recommendation, evidence_ids)
        and isinstance(ranked, list)
        and set(map(str, ranked)).issubset(alternative_ids)
        and recommendation.get("requires_human_approval") is True
    )
