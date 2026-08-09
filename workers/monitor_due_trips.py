"""Worker seam for Scheduler/PubSub assessment-due events.

The deployed worker should claim a tenant-scoped idempotency key before invoking
the API/graph; this local placeholder deliberately has no network side effect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain.models import Trip

MONITORING_WINDOWS_HOURS = (24, 6, 2)


def assessment_due_idempotency_key(*, trip_id: str, window: str) -> str:
    return f"assessment-due:{trip_id}:{window}"


def due_monitoring_windows(
    trip: Trip, *, now: datetime | None = None, grace_period: timedelta = timedelta(minutes=20)
) -> list[str]:
    """Return each pre-departure window reached in this Scheduler invocation."""
    timestamp = now or datetime.now(UTC)
    departure = trip.segments[0].scheduled_departure_at
    return [
        f"T-{hours}:{departure.isoformat()}"
        for hours in MONITORING_WINDOWS_HOURS
        if timedelta() <= timestamp - (departure - timedelta(hours=hours)) < grace_period
    ]
