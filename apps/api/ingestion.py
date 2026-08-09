"""Bounded CSV and signed-webhook validation helpers."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

REQUIRED_CSV_COLUMNS = {"tenant_id", "traveler_id", "segment_id", "carrier_code", "flight_number"}


def parse_csv_rows(content: str, *, max_rows: int = 1000) -> list[dict[str, str]]:
    rows = list(csv.DictReader(StringIO(content)))
    if not rows or not REQUIRED_CSV_COLUMNS.issubset(rows[0]):
        raise ValueError("CSV is missing required itinerary columns")
    if len(rows) > max_rows:
        raise ValueError("CSV exceeds row limit")
    if any(
        value.startswith(("=", "+", "-", "@")) for row in rows for value in row.values() if value
    ):
        raise ValueError("CSV formula-like values are not accepted")
    return rows


class BookingWebhookEvent(BaseModel):
    """Signed, minimally retained booking event envelope."""

    message_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    trip_id: UUID | None = None
    data: dict[str, object] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value


def parse_webhook_timestamp(value: str, *, max_age: timedelta = timedelta(minutes=5)) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("webhook timestamp must be ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise ValueError("webhook timestamp must include a UTC offset")
    now = datetime.now(UTC)
    if abs(now - timestamp) > max_age:
        raise ValueError("webhook timestamp is outside the accepted replay window")
    return timestamp


def parse_booking_webhook(
    body: bytes, *, timestamp: datetime, max_skew: timedelta = timedelta(minutes=5)
) -> BookingWebhookEvent:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("webhook payload must be valid JSON") from exc
    try:
        event = BookingWebhookEvent.model_validate(payload)
    except Exception as exc:
        raise ValueError("webhook payload does not match the booking event schema") from exc
    if abs(event.occurred_at - timestamp) > max_skew:
        raise ValueError("webhook body timestamp does not match the signed timestamp")
    return event


def verify_webhook_signature(
    *, body: bytes, signature: str, secret: str, timestamp: str | None = None
) -> bool:
    signed_body = f"{timestamp}.".encode() + body if timestamp else body
    expected = hmac.new(secret.encode(), signed_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
