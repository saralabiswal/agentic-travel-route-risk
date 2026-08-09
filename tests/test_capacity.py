"""Deterministic capacity/backpressure checks for local and CI release gates."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from apps.api.repository import InMemoryRouteShieldRepository
from domain.models import NotificationRecord


def test_notification_queue_claims_one_thousand_records_without_duplicate_leases():
    async def scenario() -> None:
        repository = InMemoryRouteShieldRepository()
        for number in range(1_000):
            await repository.save_notification(
                NotificationRecord(
                    tenant_id="acme",
                    traveler_id=f"traveler-{number}",
                    channel="in_app",
                    recipient_reference=f"traveler:traveler-{number}",
                    subject="Travel update",
                    body="A disruption requires review.",
                )
            )
        claims = await asyncio.gather(
            *(
                repository.claim_due_notifications(
                    tenant_id="acme",
                    worker_id=f"worker-{worker}",
                    claim_ttl=timedelta(minutes=5),
                    limit=1_000,
                )
                for worker in range(10)
            )
        )
        leased = [notification for claim in claims for notification in claim]
        assert len(leased) == 1_000
        assert len({notification.notification_id for notification in leased}) == 1_000
        assert all(notification.dispatch_claimed_by for notification in leased)

    asyncio.run(scenario())
