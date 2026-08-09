"""Retention worker contract used by a Scheduler-triggered job.

The worker delegates deletion to the repository so the same policy has identical
semantics for local tests and PostgreSQL.  It returns aggregate counts only; a
retention run must not recreate sensitive data in logs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


async def run_retention(
    repository: object,
    *,
    retention_days: int,
    original_upload_retention_days: int = 30,
    audit_retention_days: int = 365,
    now: datetime | None = None,
) -> dict[str, int]:
    if min(retention_days, original_upload_retention_days, audit_retention_days) < 1:
        raise ValueError("retention periods must be at least one day")
    timestamp = now or datetime.now(UTC)
    cutoff = timestamp - timedelta(days=retention_days)
    original_upload_cutoff = timestamp - timedelta(days=original_upload_retention_days)
    audit_cutoff = timestamp - timedelta(days=audit_retention_days)
    return await repository.purge_expired(  # type: ignore[attr-defined]
        cutoff,
        original_upload_before=original_upload_cutoff,
        audit_before=audit_cutoff,
    )
