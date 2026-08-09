"""FAA, NWS, and AviationWeather normalization contracts using captured public-source fixtures."""

from __future__ import annotations


def normalize_faa_nas(payload: dict[str, object]) -> dict[str, object]:
    events = payload.get("events", [])
    return {"risk_score": 75 if isinstance(events, list) and events else 0}


def normalize_nws_alerts(payload: dict[str, object]) -> dict[str, object]:
    features = payload.get("features", [])
    return {"risk_score": 80 if isinstance(features, list) and features else 0}


def normalize_aviation_weather(payload: dict[str, object]) -> dict[str, object]:
    hazards = payload.get("hazards", [])
    return {"risk_score": 70 if isinstance(hazards, list) and hazards else 0}
