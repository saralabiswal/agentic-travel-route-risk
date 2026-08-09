import asyncio
from datetime import datetime

from domain.models import FlightSegmentCreate, FreshnessStatus, Trip, TripCriticality
from tools.live_providers import LiveEvidenceCollector
from tools.providers import JsonHttpProvider, ProviderCache


def test_disabled_provider_returns_structured_unavailable(monkeypatch):
    monkeypatch.delenv("PROVIDER_TEST_ENABLED", raising=False)
    provider = JsonHttpProvider(
        name="test",
        source_type="weather",
        enabled_env="PROVIDER_TEST_ENABLED",
        url_env="TEST_URL",
        cache=ProviderCache(),
        normalize=lambda payload: payload,
    )
    result = asyncio.run(provider.fetch(tenant_id="acme", cache_key="SFO", correlation_id="run-1"))
    assert result.freshness_status == FreshnessStatus.UNAVAILABLE
    assert result.error_code == "provider_disabled"


def test_live_collector_respects_each_provider_kill_switch_without_network_access():
    trip = Trip(
        tenant_id="acme",
        traveler_id="traveler-1",
        trip_criticality=TripCriticality.STANDARD,
        ground_origin="1 Market St, San Francisco, CA",
        destination_country="US",
        segments=[
            FlightSegmentCreate(
                segment_id="sfo-den-1",
                carrier_code="UA",
                flight_number="123",
                departure_airport="SFO",
                arrival_airport="DEN",
                scheduled_departure_at=datetime.fromisoformat("2026-08-01T15:00:00+00:00"),
                scheduled_arrival_at=datetime.fromisoformat("2026-08-01T18:30:00+00:00"),
            )
        ],
    )

    async def disabled(_: str) -> bool:
        return False

    evidence = asyncio.run(LiveEvidenceCollector().collect(trip, "run-1", disabled))
    unavailable = [
        item for item in evidence if item.freshness_status == FreshnessStatus.UNAVAILABLE
    ]
    assert len(unavailable) == 6
    assert {item.error_code for item in unavailable} == {"provider_disabled"}
    assert any(item.source_type == "connection_feasibility" for item in evidence)
