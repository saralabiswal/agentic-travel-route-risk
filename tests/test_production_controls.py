import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.api.actions import UnconfiguredActionSender, dispatch_approved_action
from apps.api.notifications import InAppNotificationSender, deliver_notification
from apps.api.repository import InMemoryRouteShieldRepository
from apps.api.security import FixedWindowRateLimiter, redact
from domain.models import (
    ActionDispatchRecord,
    ActionDispatchStatus,
    DeletionRequest,
    DeletionRequestScope,
    DeletionRequestStatus,
    LegalHoldRecord,
    LegalHoldScope,
    NotificationRecord,
    NotificationStatus,
    TravelerPreferenceProfile,
)
from workers.privacy import process_due_deletion_requests
from workers.retention import run_retention


def test_recursive_audit_redaction_preserves_operational_context():
    assert redact(
        {
            "event": "approval.completed",
            "authorization": "Bearer secret",
            "nested": {"api_key": "secret", "safe": "value"},
            "items": [{"token": "secret"}],
        }
    ) == {
        "event": "approval.completed",
        "authorization": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "safe": "value"},
        "items": [{"token": "[REDACTED]"}],
    }


def test_rate_limit_exposes_a_retry_window():
    limiter = FixedWindowRateLimiter(limit=1, window=timedelta(minutes=1))
    assert limiter.check("tenant:actor:route").remaining == 0
    rejected = limiter.check("tenant:actor:route")
    assert not rejected.allowed
    assert rejected.retry_after_seconds > 0


def test_notification_queue_retry_delivery_and_tenant_isolation():
    async def scenario():
        repository = InMemoryRouteShieldRepository()
        in_app = NotificationRecord(
            tenant_id="acme",
            traveler_id="traveler-1",
            channel="in_app",
            recipient_reference="traveler:traveler-1",
            subject="Travel update",
            body="A manager is reviewing your trip.",
        )
        external = NotificationRecord(
            tenant_id="other",
            traveler_id="traveler-2",
            channel="email",
            recipient_reference="recipient-token",
            subject="Travel update",
            body="A manager is reviewing your trip.",
        )
        await repository.save_notification(in_app)
        await repository.save_notification(external)
        delivered = await deliver_notification(
            repository=repository, notification=in_app, sender=InAppNotificationSender()
        )
        retried = await deliver_notification(
            repository=repository, notification=external, sender=InAppNotificationSender()
        )
        assert delivered.status == NotificationStatus.DELIVERED
        assert retried.status == NotificationStatus.RETRY_SCHEDULED
        assert [item.notification_id for item in await repository.list_notifications("acme")] == [
            in_app.notification_id
        ]
        assert len(await repository.list_notification_attempts("acme", in_app.notification_id)) == 1

    asyncio.run(scenario())


def test_notification_outbox_claim_prevents_duplicate_concurrent_delivery():
    async def scenario():
        repository = InMemoryRouteShieldRepository()
        notification = NotificationRecord(
            tenant_id="acme",
            traveler_id="traveler-1",
            channel="in_app",
            recipient_reference="traveler:traveler-1",
            subject="Travel update",
            body="A manager is reviewing your trip.",
        )
        await repository.save_notification(notification)
        first, second = await asyncio.gather(
            repository.claim_due_notifications(
                tenant_id="acme",
                worker_id="worker-a",
                claim_ttl=timedelta(minutes=5),
            ),
            repository.claim_due_notifications(
                tenant_id="acme",
                worker_id="worker-b",
                claim_ttl=timedelta(minutes=5),
            ),
        )
        claimed = [*first, *second]
        assert len(claimed) == 1
        assert claimed[0].dispatch_claimed_by in {"worker-a", "worker-b"}

        delivered = await deliver_notification(
            repository=repository,
            notification=claimed[0],
            sender=InAppNotificationSender(),
        )
        assert delivered.status == NotificationStatus.DELIVERED
        assert delivered.dispatch_claim_id is None
        attempts = await repository.list_notification_attempts("acme", notification.notification_id)
        assert len(attempts) == 1

    asyncio.run(scenario())


def test_action_outbox_claim_prevents_duplicate_external_dispatch_attempts():
    async def scenario():
        repository = InMemoryRouteShieldRepository()
        action = ActionDispatchRecord(
            tenant_id="acme",
            incident_id=uuid4(),
            approval_id=uuid4(),
            action_payload={"kind": "booking_action_request"},
            idempotency_key="approved-action:one",
        )
        await repository.save_action_dispatch(action)
        first, second = await asyncio.gather(
            repository.claim_due_action_dispatches(
                tenant_id="acme",
                worker_id="worker-a",
                claim_ttl=timedelta(minutes=5),
            ),
            repository.claim_due_action_dispatches(
                tenant_id="acme",
                worker_id="worker-b",
                claim_ttl=timedelta(minutes=5),
            ),
        )
        claimed = [*first, *second]
        assert len(claimed) == 1
        completed = await dispatch_approved_action(
            repository=repository,
            action=claimed[0],
            sender=UnconfiguredActionSender(),
        )
        assert completed.status == ActionDispatchStatus.RETRY_SCHEDULED
        assert completed.dispatch_claim_id is None
        assert len(
            await repository.list_action_dispatch_attempts("acme", action.action_dispatch_id)
        ) == 1

    asyncio.run(scenario())


def test_retention_worker_deletes_only_expired_records():
    async def scenario():
        repository = InMemoryRouteShieldRepository()
        old = NotificationRecord(
            tenant_id="acme",
            traveler_id="traveler-1",
            channel="in_app",
            recipient_reference="traveler:traveler-1",
            subject="Old",
            body="Old notification",
            created_at=datetime.now(UTC) - timedelta(days=91),
        )
        fresh = old.model_copy(
            update={"notification_id": uuid4(), "created_at": datetime.now(UTC)}
        )
        await repository.save_notification(old)
        await repository.save_notification(fresh)
        result = await run_retention(repository, retention_days=90)
        assert result["notifications"] == 1
        assert len(await repository.list_notifications("acme")) == 1

    asyncio.run(scenario())


def test_legal_hold_blocks_retention_and_dsar_until_it_is_released():
    async def scenario():
        repository = InMemoryRouteShieldRepository()
        profile = TravelerPreferenceProfile(
            tenant_id="acme",
            traveler_id="traveler-1",
            consent_version="consent-v1",
            updated_at=datetime.now(UTC) - timedelta(days=91),
        )
        await repository.save_profile(profile)
        hold = LegalHoldRecord(
            tenant_id="acme",
            scope=LegalHoldScope.TRAVELER,
            traveler_id="traveler-1",
            reason="active investigation",
            created_by="privacy-admin",
        )
        await repository.save_legal_hold(hold)
        retention = await run_retention(repository, retention_days=90)
        assert retention["profiles"] == 0
        assert await repository.get_profile("acme", "traveler-1")

        request = DeletionRequest(
            tenant_id="acme",
            traveler_id="traveler-1",
            scope=DeletionRequestScope.TRAVELER_DATA,
            requested_by="traveler-1",
            reason="withdrawn consent",
        )
        await repository.save_deletion_request(request)
        blocked = await process_due_deletion_requests(repository)
        assert blocked[0][0].status == DeletionRequestStatus.BLOCKED_BY_LEGAL_HOLD
        assert await repository.get_profile("acme", "traveler-1")

        await repository.save_legal_hold(
            hold.model_copy(
                update={
                    "released_at": datetime.now(UTC),
                    "released_by": "privacy-admin",
                    "release_reason": "investigation closed",
                }
            )
        )
        completed = await process_due_deletion_requests(repository)
        assert completed[0][0].status == DeletionRequestStatus.COMPLETED
        assert await repository.get_profile("acme", "traveler-1") is None

    asyncio.run(scenario())
