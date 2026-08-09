"""Tenant-scoped request fingerprints and durable idempotency contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class IdempotencyDisposition(StrEnum):
    EXECUTE = "execute"
    REPLAY = "replay"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"


def request_fingerprint(*, scope: str, payload: Any) -> str:
    """Hash the scope and canonical JSON payload without storing raw request data."""
    canonical = json.dumps(
        {"scope": scope, "payload": payload},
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Legacy local helper retained for isolated unit tests only.

    HTTP handlers use repository-backed records so retries survive replicas and
    restarts.  This type intentionally has no production wiring.
    """
    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], tuple[datetime, object]] = {}

    def get(self, tenant_id: str, key: str) -> object | None:
        record = self._keys.get((tenant_id, key))
        return record[1] if record else None

    def put(self, tenant_id: str, key: str, result: object) -> None:
        self._keys[(tenant_id, key)] = (datetime.now(UTC), result)


class WebhookReplayStore:
    def __init__(self, max_age: timedelta = timedelta(minutes=5)) -> None:
        self.max_age = max_age
        self._seen: set[str] = set()

    def accept_once(self, message_id: str, timestamp: datetime) -> bool:
        if datetime.now(UTC) - timestamp > self.max_age or message_id in self._seen:
            return False
        self._seen.add(message_id)
        return True
