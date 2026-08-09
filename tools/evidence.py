"""Provider-neutral evidence collection contracts and local demonstration adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from domain.models import EvidenceEnvelope, FreshnessStatus, Trip


class DemoScenario(StrEnum):
    NORMAL = "normal"
    DISRUPTION = "disruption"
    SOURCE_OUTAGE = "source_outage"


class EvidenceCollector(Protocol):
    def collect_baseline(self, trip: Trip, correlation_id: str) -> list[EvidenceEnvelope]: ...


class FixtureEvidenceCollector:
    """Captured-fixture stand-in; it performs no network access and has no credentials."""

    def __init__(self, scenario: DemoScenario = DemoScenario.NORMAL) -> None:
        self.scenario = scenario

    def collect_baseline(self, trip: Trip, correlation_id: str) -> list[EvidenceEnvelope]:
        scores = {
            DemoScenario.NORMAL: {
                "flight_status": 5,
                "connection_feasibility": 10,
                "airport_weather": 5,
                "ground_route": 10,
                "destination_advisory": 15,
            },
            DemoScenario.DISRUPTION: {
                "flight_status": 90,
                "connection_feasibility": 75,
                "airport_weather": 70,
                "ground_route": 35,
                "destination_advisory": 15,
            },
            DemoScenario.SOURCE_OUTAGE: {
                "flight_status": None,
                "connection_feasibility": 10,
                "airport_weather": None,
                "ground_route": 10,
                "destination_advisory": 15,
            },
        }[self.scenario]
        now = datetime.now(UTC)
        evidence: list[EvidenceEnvelope] = []
        source_names = {
            "flight_status": "fixture-amadeus",
            "connection_feasibility": "routeshield-calculator",
            "airport_weather": "fixture-nws-aviation-weather",
            "ground_route": "fixture-google-routes",
            "destination_advisory": "fixture-state-department",
        }
        for source_type, score in scores.items():
            unavailable = score is None
            evidence.append(
                EvidenceEnvelope(
                    source_name=source_names[source_type],
                    source_type=source_type,
                    source_url_or_record_id=f"fixture:{correlation_id}:{source_type}",
                    observed_at=now,
                    expires_at=now + timedelta(minutes=15),
                    freshness_status=(
                        FreshnessStatus.UNAVAILABLE if unavailable else FreshnessStatus.FRESH
                    ),
                    normalized_payload={} if unavailable else {"risk_score": score},
                    error_code="fixture_provider_unavailable" if unavailable else None,
                )
            )
        return evidence
