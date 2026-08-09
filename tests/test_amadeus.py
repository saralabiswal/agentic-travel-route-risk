import json
from pathlib import Path

from tools.amadeus import normalize_amadeus_status


def test_delayed_amadeus_fixture_normalizes_to_elevated_risk():
    fixture = Path("fixtures/amadeus-flight-status-delayed.json")
    payload = json.loads(fixture.read_text())
    assert normalize_amadeus_status(payload) == {"risk_score": 70, "status": "DELAYED"}
