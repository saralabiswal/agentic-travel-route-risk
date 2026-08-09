from fastapi.testclient import TestClient

from apps.mock_provider.main import app

client = TestClient(app)


def test_mock_provider_has_a_health_check_and_bounded_evidence_responses():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/v1/flight-status").json()["risk_score"] == 5
    assert client.get("/v1/ground-route?risk_score=100").json()["risk_score"] == 100
