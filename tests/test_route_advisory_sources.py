import json
from pathlib import Path

from tools.route_advisory_sources import normalize_destination_advisory, normalize_google_routes


def test_route_and_advisory_fixtures_normalize():
    route = json.loads(Path("fixtures/google-routes-traffic.json").read_text())
    advisory = json.loads(Path("fixtures/destination-advisory-level-3.json").read_text())
    assert normalize_google_routes(route)["risk_score"] == 70
    assert normalize_destination_advisory(advisory)["risk_score"] == 60
