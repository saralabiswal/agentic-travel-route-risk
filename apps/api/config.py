"""Runtime controls. Environment values are fail-closed by default."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _enabled(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


def _positive_int(name: str, *, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


@dataclass(frozen=True)
class RuntimeControls:
    llm_enabled: bool
    react_tool_calls_enabled: bool
    notifications_enabled: bool
    approval_actions_enabled: bool
    memory_reads_enabled: bool
    memory_writes_enabled: bool
    require_oidc: bool
    api_rate_limit: int
    api_rate_limit_window_seconds: int
    rate_limit_redis_required: bool
    idempotency_ttl_seconds: int
    require_idempotency: bool
    tenant_automation_enabled: bool
    demo_evidence_enabled: bool
    retention_days: int

    @classmethod
    def from_environment(cls) -> RuntimeControls:
        return cls(
            llm_enabled=_enabled("LLM_ENABLED"),
            react_tool_calls_enabled=_enabled("REACT_TOOL_CALLS_ENABLED", default=True),
            notifications_enabled=_enabled("NOTIFICATIONS_ENABLED"),
            approval_actions_enabled=_enabled("APPROVAL_ACTIONS_ENABLED"),
            memory_reads_enabled=_enabled("MEMORY_READS_ENABLED", default=True),
            memory_writes_enabled=_enabled("MEMORY_WRITES_ENABLED", default=True),
            require_oidc=_enabled("REQUIRE_OIDC"),
            api_rate_limit=_positive_int("API_RATE_LIMIT", default=60),
            api_rate_limit_window_seconds=_positive_int(
                "API_RATE_LIMIT_WINDOW_SECONDS", default=60
            ),
            rate_limit_redis_required=_enabled("RATE_LIMIT_REDIS_REQUIRED"),
            idempotency_ttl_seconds=_positive_int("IDEMPOTENCY_TTL_SECONDS", default=86400),
            require_idempotency=_enabled("REQUIRE_IDEMPOTENCY"),
            tenant_automation_enabled=_enabled("TENANT_AUTOMATION_ENABLED", default=True),
            demo_evidence_enabled=_enabled("DEMO_EVIDENCE_ENABLED", default=True),
            retention_days=_positive_int("RETENTION_DAYS", default=90),
        )
