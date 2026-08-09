"""Bounded, server-side provider adapters with fixture-safe fallbacks."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from redis.asyncio import Redis

from domain.models import EvidenceEnvelope, FreshnessStatus, Trip
from tools.evidence import DemoScenario, FixtureEvidenceCollector


class EvidenceProvider(Protocol):
    async def collect(self, trip: Trip, correlation_id: str) -> list[EvidenceEnvelope]: ...


class ProviderCache:
    """Process-local cache; replace with Redis without changing adapter contracts."""

    def __init__(self) -> None:
        self._items: dict[str, EvidenceEnvelope] = {}

    def get(self, key: str) -> EvidenceEnvelope | None:
        item = self._items.get(key)
        if item and item.expires_at > datetime.now(UTC):
            return item.model_copy(update={"freshness_status": FreshnessStatus.CACHED})
        return None

    def put(self, key: str, item: EvidenceEnvelope) -> None:
        self._items[key] = item


class RedisProviderCache:
    """Optional durable cache selected only when REDIS_URL is configured."""

    def __init__(self, client: Redis) -> None:
        self.client = client

    async def get(self, key: str) -> EvidenceEnvelope | None:
        value = await self.client.get(key)
        if not value:
            return None
        return EvidenceEnvelope.model_validate_json(value).model_copy(
            update={"freshness_status": FreshnessStatus.CACHED}
        )

    async def put(self, key: str, item: EvidenceEnvelope, ttl_seconds: int) -> None:
        await self.client.set(key, item.model_dump_json(), ex=ttl_seconds)


class CircuitBreaker:
    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self.failures = 0

    @property
    def open(self) -> bool:
        return self.failures >= self.threshold

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1


class JsonHttpProvider:
    """A bounded JSON HTTP adapter; callers own payload normalization logic."""

    def __init__(
        self,
        *,
        name: str,
        source_type: str,
        enabled_env: str,
        url_env: str,
        cache: ProviderCache,
        normalize: Callable[[dict[str, object]], dict[str, object]],
        freshness_minutes: int = 15,
        timeout_seconds: float = 5,
        retries: int = 1,
    ) -> None:
        self.name = name
        self.source_type = source_type
        self.enabled_env = enabled_env
        self.url_env = url_env
        self.cache = cache
        self.normalize = normalize
        self.freshness_minutes = freshness_minutes
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.breaker = CircuitBreaker()

    async def fetch(
        self,
        *,
        tenant_id: str,
        cache_key: str,
        correlation_id: str,
        enabled: bool | None = None,
    ) -> EvidenceEnvelope:
        cached = self.cache.get(f"{tenant_id}:{self.source_type}:{cache_key}")
        if cached:
            return cached
        if self.breaker.open:
            return self._unavailable(correlation_id, "provider_circuit_open")
        if enabled is False or (
            enabled is None and os.getenv(self.enabled_env, "false").lower() != "true"
        ):
            return self._unavailable(correlation_id, "provider_disabled")
        url = os.getenv(self.url_env)
        if not url:
            return self._unavailable(correlation_id, "provider_url_not_configured")
        started_at = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                for attempt in range(self.retries + 1):
                    try:
                        response = await client.get(
                            url, headers={"X-Correlation-Id": correlation_id}
                        )
                        response.raise_for_status()
                        payload = response.json()
                        break
                    except httpx.HTTPError:
                        if attempt == self.retries:
                            raise
            if not isinstance(payload, dict):
                raise ValueError("provider JSON must be an object")
            item = EvidenceEnvelope(
                source_name=self.name,
                source_type=self.source_type,
                source_url_or_record_id=url,
                observed_at=started_at,
                expires_at=started_at + timedelta(minutes=self.freshness_minutes),
                normalized_payload=self.normalize(payload),
                provider_latency_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
            )
            self.cache.put(f"{tenant_id}:{self.source_type}:{cache_key}", item)
            self.breaker.record_success()
            return item
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            self.breaker.record_failure()
            return self._unavailable(correlation_id, type(exc).__name__.lower())

    def _unavailable(self, correlation_id: str, error_code: str) -> EvidenceEnvelope:
        now = datetime.now(UTC)
        return EvidenceEnvelope(
            source_name=self.name,
            source_type=self.source_type,
            source_url_or_record_id=f"unavailable:{correlation_id}",
            observed_at=now,
            expires_at=now,
            freshness_status=FreshnessStatus.UNAVAILABLE,
            error_code=error_code,
        )


class FixtureProvider:
    """Captured fixture adapter used by tests and the credential-free local demo."""

    def __init__(self, scenario: DemoScenario = DemoScenario.NORMAL) -> None:
        self._collector = FixtureEvidenceCollector(scenario)

    async def collect(self, trip: Trip, correlation_id: str) -> list[EvidenceEnvelope]:
        return self._collector.collect_baseline(trip, correlation_id)
