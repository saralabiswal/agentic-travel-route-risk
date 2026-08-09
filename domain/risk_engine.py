"""Explainable deterministic route-risk scoring. This module never calls an LLM."""

from __future__ import annotations

from domain.models import RiskAssessment, RiskFactors, Severity

RISK_POLICY_VERSION = "mvp-2026-07-14"
WEIGHTS = {
    "flight_disruption": 0.35,
    "connection_fragility": 0.20,
    "airport_weather": 0.20,
    "ground_route_disruption": 0.10,
    "destination_advisory": 0.10,
    "traveler_trip_criticality": 0.05,
}


def severity_for_score(score: float) -> Severity:
    if score < 25:
        return Severity.LOW
    if score < 50:
        return Severity.WATCH
    if score < 75:
        return Severity.HIGH
    return Severity.CRITICAL


def calculate_risk(
    *, trip_id, factors: RiskFactors, evidence_ids: list, unknown_factors: list[str] | None = None
) -> RiskAssessment:
    # Scores are calculated from this fixed, versioned policy rather than an LLM response.
    # Keeping each contribution makes an assessment explainable in the API and audit record.
    contributions = {
        factor: round(getattr(factors, factor) * weight, 2) for factor, weight in WEIGHTS.items()
    }
    score = round(sum(contributions.values()), 2)
    unknown_factors = unknown_factors or []
    return RiskAssessment(
        trip_id=trip_id,
        policy_version=RISK_POLICY_VERSION,
        risk_score=score,
        severity=severity_for_score(score),
        factor_contributions=contributions,
        evidence_ids=evidence_ids,
        unknown_factors=unknown_factors,
        uncertainty="high" if unknown_factors else "low",
    )
