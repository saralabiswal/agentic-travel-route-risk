"""A deterministic, local HTTP stand-in for the enabled RouteShield evidence adapters.

It deliberately exposes only normalized, non-sensitive risk evidence.  It is useful
for exercising the API's real HTTP timeout/retry/cache paths in Docker Compose; it is
not a substitute for a reviewed production provider adapter.
"""

from __future__ import annotations

from fastapi import FastAPI, Query

app = FastAPI(title="RouteShield Mock Provider", version="1.0.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "routeshield-mock-provider"}


def response(*, source: str, default_score: float, risk_score: float | None) -> dict[str, object]:
    """Return a bounded score so local clients exercise the production contract."""
    score = default_score if risk_score is None else risk_score
    return {"source": source, "risk_score": min(100, max(0, score))}


@app.get("/v1/flight-status")
async def flight_status(
    risk_score: float | None = Query(default=None, ge=0, le=100),
) -> dict[str, object]:
    return response(source="mock-flight-status", default_score=5, risk_score=risk_score)


@app.get("/v1/airport-weather")
async def airport_weather(
    risk_score: float | None = Query(default=None, ge=0, le=100),
) -> dict[str, object]:
    return response(source="mock-airport-weather", default_score=10, risk_score=risk_score)


@app.get("/v1/ground-route")
async def ground_route(
    risk_score: float | None = Query(default=None, ge=0, le=100),
) -> dict[str, object]:
    return response(source="mock-ground-route", default_score=10, risk_score=risk_score)


@app.get("/v1/destination-advisory")
async def destination_advisory(
    risk_score: float | None = Query(default=None, ge=0, le=100),
) -> dict[str, object]:
    return response(source="mock-destination-advisory", default_score=15, risk_score=risk_score)
