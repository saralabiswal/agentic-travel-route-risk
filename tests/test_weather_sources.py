import json
from pathlib import Path

from tools.weather_sources import (
    normalize_aviation_weather,
    normalize_faa_nas,
    normalize_nws_alerts,
)


def fixture(name: str) -> dict[str, object]:
    return json.loads(Path("fixtures", name).read_text())


def test_public_weather_fixtures_normalize_to_risk_scores():
    assert normalize_faa_nas(fixture("faa-nas-delay.json"))["risk_score"] == 75
    assert normalize_nws_alerts(fixture("nws-alert.json"))["risk_score"] == 80
    assert normalize_aviation_weather(fixture("aviation-weather-hazard.json"))["risk_score"] == 70
