import asyncio
from concurrent.futures import ThreadPoolExecutor

from apps.api.repository import InMemoryRouteShieldRepository
from apps.api.security import FixedWindowRateLimiter
from domain.models import NotificationRecord


def test_rate_limit_backstop_is_thread_safe_under_burst_load():
    limiter = FixedWindowRateLimiter(limit=40)
    with ThreadPoolExecutor(max_workers=16) as executor:
        accepted = list(
            executor.map(lambda _: limiter.allow("acme:actor:/v1/incidents"), range(200))
        )
    assert sum(accepted) == 40


def test_notification_tenant_isolation_survives_serialized_restore_round_trip():
    async def scenario():
        source = NotificationRecord(
            tenant_id="acme",
            traveler_id="traveler-1",
            channel="in_app",
            recipient_reference="traveler:traveler-1",
            subject="Travel update",
            body="Your manager is reviewing a disruption.",
        )
        restored = NotificationRecord.model_validate(source.model_dump(mode="json"))
        repository = InMemoryRouteShieldRepository()
        await repository.save_notification(restored)
        assert [item.notification_id for item in await repository.list_notifications("acme")] == [
            source.notification_id
        ]
        assert await repository.list_notifications("another-tenant") == []

    asyncio.run(scenario())
