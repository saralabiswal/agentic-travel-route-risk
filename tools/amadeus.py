"""Amadeus Flight Status adapter with a captured-fixture normalization contract."""

from __future__ import annotations

import os

from tools.live_providers import amadeus_flight_status
from tools.providers import ProviderCache


def normalize_amadeus_status(payload: dict[str, object]) -> dict[str, object]:
    """Map a captured Amadeus status response to the risk-engine input contract."""
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return {"risk_score": 0, "status": "unknown"}
    flight = data[0]
    status = str(flight.get("status", "UNKNOWN")).upper()
    score = {"SCHEDULED": 5, "ACTIVE": 15, "LANDED": 0, "CANCELLED": 100, "DELAYED": 70}.get(
        status, 30
    )
    return {"risk_score": score, "status": status}


def flight_status_adapter(cache: ProviderCache) -> object:
    """Return the disabled-by-default HTTP adapter configured for Amadeus."""
    return amadeus_flight_status(cache)


def amadeus_configured() -> bool:
    return bool(
        os.getenv("AMADEUS_CLIENT_ID")
        and os.getenv("AMADEUS_CLIENT_SECRET")
        and os.getenv("AMADEUS_FLIGHT_STATUS_URL")
    )
