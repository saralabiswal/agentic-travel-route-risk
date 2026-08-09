from fastapi.testclient import TestClient

from apps.web.server import app

client = TestClient(app)


def test_console_explains_the_incident_decision_flow():
    response = client.get("/console/")
    assert response.status_code == 200
    assert "Work one incident from signal to safe action" in response.text
    assert "Review priority queue" in response.text
    assert "Understand and decide" in response.text
    assert "Communicate clearly" in response.text


def test_console_javascript_has_a_human_readable_connection_error():
    response = client.get("/console/app.js")
    assert response.status_code == 200
    assert "Cannot reach" in response.text
    assert "Confirm the local API is running" in response.text
