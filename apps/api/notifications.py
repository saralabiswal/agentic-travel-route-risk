"""Durable notification queue processing with bounded retries.

The service owns queue state and acknowledgement records.  Channel providers are
small adapters behind ``NotificationSender`` so a provider never receives more
than the approved, rendered message and opaque recipient reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domain.models import NotificationAttempt, NotificationRecord, NotificationStatus


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    provider_message_reference: str | None = None
    error_code: str | None = None


class NotificationSender(Protocol):
    async def send(self, notification: NotificationRecord) -> DeliveryResult: ...


class InAppNotificationSender:
    """A safe sender for the in-product inbox channel.

    External email/SMS/push providers must be attached through a reviewed adapter
    after their credentials and notification policy are approved.  Treating the
    record itself as the in-app inbox delivery gives production deployments a
    useful channel without a hidden external side effect.
    """

    async def send(self, notification: NotificationRecord) -> DeliveryResult:
        if notification.channel == "in_app":
            return DeliveryResult(
                delivered=True,
                provider_message_reference=f"in-app:{notification.notification_id}",
            )
        return DeliveryResult(delivered=False, error_code="channel_not_configured")


def retry_at(*, attempt_number: int, now: datetime) -> datetime:
    """Return an exponential delay capped at one hour."""
    delay_minutes = min(60, 2 ** max(0, attempt_number - 1))
    return now + timedelta(minutes=delay_minutes)


async def deliver_notification(
    *,
    repository: object,
    notification: NotificationRecord,
    sender: NotificationSender,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> NotificationRecord:
    """Attempt delivery once and atomically persist its queue transition.

    Repository methods are structural here to keep the worker compatible with
    both the in-memory test repository and the PostgreSQL repository.
    """
    if notification.status not in {NotificationStatus.QUEUED, NotificationStatus.RETRY_SCHEDULED}:
        return notification
    timestamp = now or datetime.now(UTC)
    if notification.next_attempt_at > timestamp:
        return notification
    claim_id = notification.dispatch_claim_id
    if (
        notification.dispatch_claim_expires_at
        and notification.dispatch_claim_expires_at <= timestamp
    ):
        return notification

    attempt_number = notification.attempt_count + 1
    try:
        result = await sender.send(notification)
    except Exception:
        result = DeliveryResult(delivered=False, error_code="sender_exception")
    notification.attempt_count = attempt_number
    notification.updated_at = timestamp
    if result.delivered:
        notification.status = NotificationStatus.DELIVERED
        notification.delivered_at = timestamp
        notification.last_error_code = None
        outcome = "delivered"
    elif attempt_number >= max_attempts:
        notification.status = NotificationStatus.FAILED
        notification.last_error_code = result.error_code or "delivery_failed"
        outcome = "failed"
    else:
        notification.status = NotificationStatus.RETRY_SCHEDULED
        notification.next_attempt_at = retry_at(attempt_number=attempt_number, now=timestamp)
        notification.last_error_code = result.error_code or "delivery_failed"
        outcome = "retry_scheduled"

    attempt = NotificationAttempt(
        notification_id=notification.notification_id,
        attempt_number=attempt_number,
        outcome=outcome,
        provider_message_reference=result.provider_message_reference,
        error_code=result.error_code,
        attempted_at=timestamp,
    )
    if claim_id:
        completed = await repository.complete_notification_delivery(  # type: ignore[attr-defined]
            notification=notification,
            attempt=attempt,
            claim_id=claim_id,
        )
        return completed or notification
    await repository.save_notification(notification)  # type: ignore[attr-defined]
    await repository.record_notification_attempt(attempt)  # type: ignore[attr-defined]
    return notification
