"""Rate limiting and safe audit-payload handling.

The helpers in this module deliberately do not write request bodies to logs.  Audit
payloads are reduced to an allow-listed shape by callers and then passed through
``redact`` before they can reach a repository or a log sink.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

SENSITIVE_FIELDS = {
    "authorization",
    "cookie",
    "set-cookie",
    "openai_api_key",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "x-api-key",
    "client_secret",
    "private_key",
    "signature",
}


def _sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in {item.replace("-", "_") for item in SENSITIVE_FIELDS}


def redact(payload: Any) -> Any:
    """Recursively remove credentials from a JSON-compatible audit payload.

    Redaction is intentionally key-based: identifiers and operational metadata
    remain useful to an incident responder, while values below a secret-shaped key
    can never be persisted.  Lists and nested objects are handled so a secret
    cannot escape through a provider response or an action payload.
    """
    if isinstance(payload, dict):
        return {
            str(key): "[REDACTED]" if _sensitive_key(key) else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple, set)):
        return [redact(value) for value in payload]
    return payload


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """Thread-safe local limiter used for development and test deployments.

    Production replicas should use a shared edge/API-gateway limit or the Redis
    adapter supplied by the deployment.  This implementation is still useful as
    a fail-closed, per-process backstop and exposes response metadata consistently.
    """

    def __init__(self, limit: int = 60, window: timedelta = timedelta(minutes=1)) -> None:
        if limit < 1:
            raise ValueError("rate limit must be at least one request")
        if window <= timedelta(0):
            raise ValueError("rate-limit window must be positive")
        self.limit, self.window = limit, window
        self.requests: dict[str, list[datetime]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        return self.check(key).allowed

    def check(self, key: str) -> RateLimitResult:
        now = datetime.now(UTC)
        with self._lock:
            active = [time for time in self.requests[key] if now - time < self.window]
            self.requests[key] = active
            if len(active) >= self.limit:
                retry_after = max(1, int((self.window - (now - active[0])).total_seconds()))
                return RateLimitResult(
                    allowed=False,
                    limit=self.limit,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )
            active.append(now)
            return RateLimitResult(
                allowed=True,
                limit=self.limit,
                remaining=self.limit - len(active),
                retry_after_seconds=0,
            )


class RedisFixedWindowRateLimiter:
    """Shared rate limiter for multi-replica deployments.

    Requests are counted in a Redis key per identity/route/window.  The caller
    decides whether a Redis failure is fail-closed (the production default) or
    may fall back to the process-local limiter for development.
    """

    def __init__(self, redis_url: str, *, limit: int, window: timedelta) -> None:
        if limit < 1 or window <= timedelta(0):
            raise ValueError("Redis rate limiter requires positive limit and window")
        self.client: Redis = Redis.from_url(redis_url, decode_responses=True)
        self.limit = limit
        self.window_seconds = max(1, int(window.total_seconds()))

    async def check(self, key: str) -> RateLimitResult:
        now = datetime.now(UTC)
        epoch = int(now.timestamp())
        window_start = epoch - (epoch % self.window_seconds)
        redis_key = f"routeshield:rate-limit:{window_start}:{key}"
        count = await self.client.incr(redis_key)
        if count == 1:
            await self.client.expire(redis_key, self.window_seconds + 1)
        retry_after = max(1, self.window_seconds - (epoch - window_start))
        return RateLimitResult(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            retry_after_seconds=retry_after if count > self.limit else 0,
        )

    async def ping(self) -> None:
        try:
            await self.client.ping()
        except RedisError:
            await self.client.aclose()
            raise

    async def close(self) -> None:
        await self.client.aclose()
