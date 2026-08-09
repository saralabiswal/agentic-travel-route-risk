"""Source-specific RouteShield provider adapters built on the bounded HTTP primitive."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from domain.models import EvidenceEnvelope, FreshnessStatus, Trip
from tools.providers import JsonHttpProvider, ProviderCache


def risk_score_payload(payload: dict[str, object]) -> dict[str, object]:
    """Accept only a normalized numeric score from an adapter/parser boundary."""
    score = payload.get("risk_score", 0)
    return {"risk_score": float(score) if isinstance(score, int | float) else 0}


def amadeus_flight_status(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="amadeus-flight-status",
        source_type="flight_status",
        enabled_env="PROVIDER_AMADEUS_ENABLED",
        url_env="AMADEUS_FLIGHT_STATUS_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=5,
    )


def nws_alerts(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="nws-alerts",
        source_type="airport_weather",
        enabled_env="PROVIDER_NWS_ENABLED",
        url_env="NWS_ALERTS_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=15,
    )


def faa_nas(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="faa-nas-status",
        source_type="airport_weather",
        enabled_env="PROVIDER_FAA_ENABLED",
        url_env="FAA_NAS_STATUS_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=15,
    )


def aviation_weather(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="aviation-weather",
        source_type="airport_weather",
        enabled_env="PROVIDER_AVIATION_WEATHER_ENABLED",
        url_env="AVIATION_WEATHER_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=15,
    )


def google_routes(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="google-routes",
        source_type="ground_route",
        enabled_env="PROVIDER_GOOGLE_ROUTES_ENABLED",
        url_env="GOOGLE_ROUTES_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=5,
    )


def destination_advisory(cache: ProviderCache) -> JsonHttpProvider:
    return JsonHttpProvider(
        name="state-department-advisory",
        source_type="destination_advisory",
        enabled_env="PROVIDER_DESTINATION_ADVISORY_ENABLED",
        url_env="DESTINATION_ADVISORY_URL",
        cache=cache,
        normalize=risk_score_payload,
        freshness_minutes=1440,
    )


class LiveEvidenceCollector:
    """Collect bounded, normalized provider evidence for one tenant-scoped assessment."""

    def __init__(self, cache: ProviderCache | None = None) -> None:
        provider_cache = cache or ProviderCache()
        self.providers = (
            ("PROVIDER_AMADEUS_ENABLED", amadeus_flight_status(provider_cache)),
            ("PROVIDER_FAA_ENABLED", faa_nas(provider_cache)),
            ("PROVIDER_NWS_ENABLED", nws_alerts(provider_cache)),
            ("PROVIDER_AVIATION_WEATHER_ENABLED", aviation_weather(provider_cache)),
            ("PROVIDER_GOOGLE_ROUTES_ENABLED", google_routes(provider_cache)),
            ("PROVIDER_DESTINATION_ADVISORY_ENABLED", destination_advisory(provider_cache)),
        )

    async def collect(
        self,
        trip: Trip,
        correlation_id: str,
        provider_enabled: Callable[[str], Awaitable[bool]],
    ) -> list[EvidenceEnvelope]:
        first_segment = trip.segments[0]
        # The cache key uses operational fields only. Tenant isolation is added by the provider
        # cache layer, so a traveler identifier is never used as a cache key component.
        cache_key = ":".join(
            [
                first_segment.carrier_code,
                first_segment.flight_number,
                first_segment.departure_airport,
                trip.destination_country,
            ]
        )
        # Resolve tenant runtime switches before starting requests. A disabled source still
        # returns an explicit UNAVAILABLE evidence envelope; it is never silently omitted.
        enabled = await asyncio.gather(
            *(provider_enabled(flag) for flag, _ in self.providers)
        )
        # Provider adapters catch their own network and parsing failures, allowing one slow or
        # unavailable source to degrade the assessment safely without blocking other evidence.
        evidence = await asyncio.gather(
            *(
                provider.fetch(
                    tenant_id=trip.tenant_id,
                    cache_key=cache_key,
                    correlation_id=correlation_id,
                    enabled=provider_enabled_value,
                )
                for provider_enabled_value, (_, provider) in zip(enabled, self.providers)
            )
        )
        # Connection feasibility is calculated locally from the normalized itinerary, rather
        # than delegated to an external provider or a model.
        evidence.append(self._connection_feasibility(trip, correlation_id))
        return evidence

    @staticmethod
    def _connection_feasibility(trip: Trip, correlation_id: str) -> EvidenceEnvelope:
        connection_minutes = min(
            (
                (
                    next_segment.scheduled_departure_at - previous.scheduled_arrival_at
                ).total_seconds()
                / 60
                for previous, next_segment in zip(trip.segments, trip.segments[1:])
            ),
            default=90,
        )
        risk_score = 90 if connection_minutes < 45 else 40 if connection_minutes < 60 else 5
        now = datetime.now(UTC)
        return EvidenceEnvelope(
            source_name="routeshield-connection-feasibility",
            source_type="connection_feasibility",
            source_url_or_record_id=f"calculated:{correlation_id}",
            observed_at=now,
            expires_at=now + timedelta(minutes=15),
            freshness_status=FreshnessStatus.FRESH,
            normalized_payload={"risk_score": risk_score, "connection_minutes": connection_minutes},
        )
