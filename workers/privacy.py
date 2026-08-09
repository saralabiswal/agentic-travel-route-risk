"""Idempotent privacy-deletion processing for Scheduler-triggered execution."""

from __future__ import annotations

from datetime import UTC, datetime

from domain.models import DeletionRequest, DeletionRequestStatus


async def process_due_deletion_requests(
    repository: object, *, now: datetime | None = None
) -> list[tuple[DeletionRequest, dict[str, int]]]:
    """Apply pending DSAR requests unless a currently active legal hold matches.

    The repository operation is idempotent and status is saved after every
    result, so an interrupted worker safely retries without reintroducing data.
    Payloads are deliberately omitted from the return value.
    """
    timestamp = now or datetime.now(UTC)
    processed: list[tuple[DeletionRequest, dict[str, int]]] = []
    requests = await repository.list_pending_deletion_requests()  # type: ignore[attr-defined]
    for request in requests:
        holds = await repository.active_legal_holds_for_traveler(  # type: ignore[attr-defined]
            request.tenant_id, request.traveler_id, now=timestamp
        )
        if holds:
            request.status = DeletionRequestStatus.BLOCKED_BY_LEGAL_HOLD
            request.blocked_by_hold_ids = [hold.legal_hold_id for hold in holds]
            request.failure_code = None
            await repository.save_deletion_request(request)  # type: ignore[attr-defined]
            processed.append((request, {}))
            continue
        try:
            deleted = await repository.erase_traveler_data(  # type: ignore[attr-defined]
                request.tenant_id, request.traveler_id, scope=request.scope
            )
        except Exception:
            request.status = DeletionRequestStatus.FAILED
            request.failure_code = "deletion_execution_failed"
            await repository.save_deletion_request(request)  # type: ignore[attr-defined]
            processed.append((request, {}))
            continue
        request.status = DeletionRequestStatus.COMPLETED
        request.completed_at = timestamp
        request.blocked_by_hold_ids = []
        request.failure_code = None
        await repository.save_deletion_request(request)  # type: ignore[attr-defined]
        processed.append((request, deleted))
    return processed
