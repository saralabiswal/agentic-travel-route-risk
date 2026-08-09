import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from apps.api.main import app, repository


def signed_headers(body: bytes, timestamp: str) -> dict[str, str]:
    signature = hmac.new(
        b"booking-secret", f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": timestamp,
        "X-Tenant-Id": "acme",
    }


def setup_function():
    repository.trips.clear()
    repository.evidence.clear()
    repository.assessments.clear()
    repository.events.clear()
    repository.idempotency_records.clear()


def test_booking_webhook_validates_schema_timestamp_and_replay(monkeypatch):
    monkeypatch.setenv("BOOKING_WEBHOOK_SECRET", "booking-secret")
    client = TestClient(app)
    timestamp = datetime.now(UTC).isoformat()
    body = json.dumps(
        {
            "message_id": "provider-event-1",
            "tenant_id": "acme",
            "event_type": "trip.updated",
            "occurred_at": timestamp,
            "data": {"change": "schedule"},
        }
    ).encode()
    headers = signed_headers(body, timestamp)

    first = client.post("/v1/webhooks/booking", content=body, headers=headers)
    replay = client.post("/v1/webhooks/booking", content=body, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert len(repository.events) == 1

    altered = json.dumps(
        {
            "message_id": "provider-event-1",
            "tenant_id": "acme",
            "event_type": "trip.cancelled",
            "occurred_at": timestamp,
        }
    ).encode()
    conflict = client.post(
        "/v1/webhooks/booking", content=altered, headers=signed_headers(altered, timestamp)
    )
    assert conflict.status_code == 409

    expired = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    expired_body = json.dumps(
        {
            "message_id": "provider-event-2",
            "tenant_id": "acme",
            "event_type": "trip.updated",
            "occurred_at": expired,
        }
    ).encode()
    rejected = client.post(
        "/v1/webhooks/booking", content=expired_body, headers=signed_headers(expired_body, expired)
    )
    assert rejected.status_code == 422


def test_booking_itinerary_upsert_normalizes_a_trip_and_starts_a_baseline_assessment(monkeypatch):
    monkeypatch.setenv("BOOKING_WEBHOOK_SECRET", "booking-secret")
    client = TestClient(app)
    timestamp = datetime.now(UTC).isoformat()
    body = json.dumps(
        {
            "message_id": "provider-itinerary-1",
            "tenant_id": "acme",
            "event_type": "itinerary.upsert",
            "occurred_at": timestamp,
            "data": {
                "trip": {
                    "tenant_id": "acme",
                    "traveler_id": "traveler-1",
                    "trip_criticality": "standard",
                    "ground_origin": "1 Market St, San Francisco, CA",
                    "destination_country": "US",
                    "segments": [
                        {
                            "segment_id": "sfo-den-1",
                            "carrier_code": "UA",
                            "flight_number": "123",
                            "departure_airport": "SFO",
                            "arrival_airport": "DEN",
                            "scheduled_departure_at": "2026-08-01T15:00:00Z",
                            "scheduled_arrival_at": "2026-08-01T18:30:00Z",
                        }
                    ],
                }
            },
        }
    ).encode()
    headers = signed_headers(body, timestamp)

    first = client.post("/v1/webhooks/booking", content=body, headers=headers)
    replay = client.post("/v1/webhooks/booking", content=body, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["trip_id"]
    assert first.json()["assessment_id"]
    assert len(repository.trips) == 1
    assert len(repository.assessments) == 1


def test_booking_material_change_contracts_update_and_cancel_the_existing_itinerary(monkeypatch):
    monkeypatch.setenv("BOOKING_WEBHOOK_SECRET", "booking-secret")
    client = TestClient(app)
    timestamp = datetime.now(UTC).isoformat()
    trip = {
        "tenant_id": "acme",
        "traveler_id": "traveler-1",
        "trip_criticality": "standard",
        "ground_origin": "1 Market St, San Francisco, CA",
        "destination_country": "US",
        "segments": [
            {
                "segment_id": "sfo-den-1",
                "carrier_code": "UA",
                "flight_number": "123",
                "departure_airport": "SFO",
                "arrival_airport": "DEN",
                "scheduled_departure_at": "2026-08-01T15:00:00Z",
                "scheduled_arrival_at": "2026-08-01T18:30:00Z",
            }
        ],
    }
    created_body = json.dumps(
        {
            "message_id": "provider-create-1",
            "tenant_id": "acme",
            "event_type": "itinerary.upsert",
            "occurred_at": timestamp,
            "data": {"trip": trip},
        }
    ).encode()
    created = client.post(
        "/v1/webhooks/booking",
        content=created_body,
        headers=signed_headers(created_body, timestamp),
    )
    trip_id = created.json()["trip_id"]

    updated_trip = {**trip, "segments": [{**trip["segments"][0], "flight_number": "999"}]}
    updated_body = json.dumps(
        {
            "message_id": "provider-update-1",
            "tenant_id": "acme",
            "event_type": "itinerary.updated",
            "occurred_at": timestamp,
            "trip_id": trip_id,
            "data": {"trip": updated_trip},
        }
    ).encode()
    updated = client.post(
        "/v1/webhooks/booking",
        content=updated_body,
        headers=signed_headers(updated_body, timestamp),
    )
    assert updated.status_code == 202
    assert updated.json()["material_change"] == "itinerary_updated"
    assert repository.trips[UUID(trip_id)].segments[0].flight_number == "999"

    cancelled_body = json.dumps(
        {
            "message_id": "provider-cancel-1",
            "tenant_id": "acme",
            "event_type": "itinerary.cancelled",
            "occurred_at": timestamp,
            "trip_id": trip_id,
        }
    ).encode()
    cancelled = client.post(
        "/v1/webhooks/booking",
        content=cancelled_body,
        headers=signed_headers(cancelled_body, timestamp),
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["material_change"] == "itinerary_cancelled"
    assert repository.trips[UUID(trip_id)].status == "cancelled"
    assert any(
        item.normalized_payload.get("booking_status") == "cancelled"
        for item in repository.evidence[UUID(trip_id)]
    )
