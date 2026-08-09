"""Claim-safe dispatch of human-approved external action requests.

No booking or payment adapter is enabled by default. A reviewed adapter must use
the durable action dispatch ID as its provider idempotency key before it can be
configured in this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domain.models import (
    ActionDispatchAttempt,
    ActionDispatchRecord,
    ActionDispatchStatus,
)


@dataclass(frozen=True)
class ActionDispatchResult:
    dispatched: bool
    external_reference: str | None = None
    error_code: str | None = None


class ApprovedActionSender(Protocol):
    async def dispatch(self, action: ActionDispatchRecord) -> ActionDispatchResult: ...


class UnconfiguredActionSender:
    """Fail closed until a tenant-approved provider adapter is explicitly installed."""

    async def dispatch(self, action: ActionDispatchRecord) -> ActionDispatchResult:
        del action
        return ActionDispatchResult(dispatched=False, error_code="action_provider_not_configured")


def retry_at(*, attempt_number: int, now: datetime) -> datetime:
    return now + timedelta(minutes=min(60, 2 ** max(0, attempt_number - 1)))


async def dispatch_approved_action(
    *,
    repository: object,
    action: ActionDispatchRecord,
    sender: ApprovedActionSender,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> ActionDispatchRecord:
    """Dispatch one leased outbox action and atomically record its outcome."""
    if action.status not in {ActionDispatchStatus.QUEUED, ActionDispatchStatus.RETRY_SCHEDULED}:
        return action
    timestamp = now or datetime.now(UTC)
    if action.next_attempt_at > timestamp:
        return action
    claim_id = action.dispatch_claim_id
    if action.dispatch_claim_expires_at and action.dispatch_claim_expires_at <= timestamp:
        return action

    attempt_number = action.attempt_count + 1
    try:
        result = await sender.dispatch(action)
    except Exception:
        result = ActionDispatchResult(dispatched=False, error_code="action_sender_exception")
    action.attempt_count = attempt_number
    action.updated_at = timestamp
    if result.dispatched:
        action.status = ActionDispatchStatus.DISPATCHED
        action.dispatched_at = timestamp
        action.last_error_code = None
        outcome = "dispatched"
    elif attempt_number >= max_attempts:
        action.status = ActionDispatchStatus.FAILED
        action.last_error_code = result.error_code or "action_dispatch_failed"
        outcome = "failed"
    else:
        action.status = ActionDispatchStatus.RETRY_SCHEDULED
        action.next_attempt_at = retry_at(attempt_number=attempt_number, now=timestamp)
        action.last_error_code = result.error_code or "action_dispatch_failed"
        outcome = "retry_scheduled"

    attempt = ActionDispatchAttempt(
        action_dispatch_id=action.action_dispatch_id,
        attempt_number=attempt_number,
        outcome=outcome,
        external_reference=result.external_reference,
        error_code=result.error_code,
        attempted_at=timestamp,
    )
    if claim_id:
        completed = await repository.complete_action_dispatch(  # type: ignore[attr-defined]
            action=action,
            attempt=attempt,
            claim_id=claim_id,
        )
        return completed or action
    await repository.save_action_dispatch(action)  # type: ignore[attr-defined]
    await repository.record_action_dispatch_attempt(attempt)  # type: ignore[attr-defined]
    return action
