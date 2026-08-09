"""Google Routes and State Department advisory fixture normalization contracts."""

from __future__ import annotations


def normalize_google_routes(payload: dict[str, object]) -> dict[str, object]:
    duration = payload.get("traffic_delay_minutes", 0)
    return {"risk_score": min(100, float(duration) * 2) if isinstance(duration, int | float) else 0}


def normalize_destination_advisory(payload: dict[str, object]) -> dict[str, object]:
    level = payload.get("advisory_level", 1)
    return {"risk_score": min(100, max(0, (int(level) - 1) * 30)) if isinstance(level, int) else 0}
